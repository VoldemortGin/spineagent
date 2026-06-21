"""LLM provider 适配器:把真实后端接进 corespine 的 LLMProvider 缝。

家族缝的元模式(同 mcp / a2a):Protocol(在 corespine)+ 离线确定性默认(corespine MockProvider)
+ Registry 工厂 + 真实后端经可选 extra 延迟 import。本模块只提供【两个真实适配器 + 工厂】:

  - AnthropicProvider     —— 走官方 `anthropic` SDK 的 messages.create(默认 claude-opus-4-8);
  - OpenAICompatProvider  —— 走官方 `openai` SDK 的 chat.completions.create + 可配 base_url,
                             一个适配器覆盖 OpenAI 及所有「OpenAI 兼容」端点
                             (Together / Groq / DeepSeek / Ollama / vLLM / Azure …)。

【为何不是 LangChain / 不做 shim】「统一 invoke」就是 corespine 的 `LLMProvider.complete()`——
LlmAgent 全程只认它。两家 API 形状不同(Anthropic 的 system 单独 + content blocks;OpenAI 的
messages + choices),各适配器各做【原生】映射,绝不把 Claude 套进 OpenAI 形状(那会丢 thinking /
content blocks / refusal 等)。这正是「敢放手填广度、却让缝稳」的做法,无需任何外部胖框架。

【import-clean】本模块顶层【绝不】import anthropic / openai;真实 SDK 仅在【未注入 client】且
构造适配器时经 corespine.lazy_extra_import 延迟 import,缺 extra 给「pip install agentspine[…]」
友好报错。映射逻辑可注入 fake client 离线单测,真实路径只是那一行延迟 import。
"""

from __future__ import annotations

from typing import Any

from corespine.llm.provider import Completion, LLMProvider, MockProvider
from corespine.seam.registry import Registry, lazy_extra_import

# 真实 SDK 的 import 名(装了对应 extra 才有);默认离线路径绝不 import 它们。
_ANTHROPIC_SDK_MODULE = "anthropic"
_OPENAI_SDK_MODULE = "openai"


def load_anthropic_sdk() -> Any:
    """延迟 import 官方 anthropic SDK;未装 [anthropic] extra 时给友好安装指引。"""
    return lazy_extra_import(_ANTHROPIC_SDK_MODULE, pkg="agentspine", extra="anthropic")


def load_openai_sdk() -> Any:
    """延迟 import 官方 openai SDK;未装 [openai] extra 时给友好安装指引。"""
    return lazy_extra_import(_OPENAI_SDK_MODULE, pkg="agentspine", extra="openai")


class AnthropicProvider:
    """走官方 anthropic SDK 的 LLMProvider 适配器(默认 claude-opus-4-8)。

    构造:未注入 client 时延迟 import anthropic 并建 `Anthropic(**client_kwargs)`(api_key 默认
    从环境 ANTHROPIC_API_KEY 读)。`extra` 透传给 messages.create(如需 thinking / 流式等自行加)。
    complete:把 prompt 包成单条 user 消息、system 单独传,取回 content 里的 text block 拼成文本,
    并映射 usage(input/output tokens)。映射是纯文本搬运,不预设 thinking / 工具调用(留待 Protocol
    最小扩展时再长)。
    """

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-8",
        max_tokens: int = 4096,
        client: Any = None,
        extra: dict[str, Any] | None = None,
        **client_kwargs: Any,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._extra = dict(extra or {})
        if client is None:
            sdk = load_anthropic_sdk()  # 仅在未注入 client 时才拉真实 SDK
            client = sdk.Anthropic(**client_kwargs)
        self._client = client

    def complete(self, prompt: str, *, system: str = "") -> Completion:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            **self._extra,
        )
        # content 是 block 列表;只取 text block(thinking 等非文本 block 跳过)。
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return Completion(text=text, usage=usage)


class OpenAICompatProvider:
    """走官方 openai SDK 的 LLMProvider 适配器,可配 base_url 覆盖一切「OpenAI 兼容」端点。

    model 必填(各兼容端点的模型名不同,无通用默认)。base_url 指向兼容端点(留空即官方 OpenAI)。
    complete:把 system(若有)+ prompt 组成 messages,调 chat.completions.create,取
    choices[0].message.content,映射 usage(prompt/completion tokens)。
    """

    def __init__(
        self,
        model: str,
        *,
        max_tokens: int = 4096,
        client: Any = None,
        base_url: str | None = None,
        extra: dict[str, Any] | None = None,
        **client_kwargs: Any,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._extra = dict(extra or {})
        if client is None:
            sdk = load_openai_sdk()  # 仅在未注入 client 时才拉真实 SDK
            client = sdk.OpenAI(base_url=base_url, **client_kwargs)
        self._client = client

    def complete(self, prompt: str, *, system: str = "") -> Completion:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=self._max_tokens,
            **self._extra,
        )
        text = response.choices[0].message.content or ""
        usage = {
            "input_tokens": response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
        }
        return Completion(text=text, usage=usage)


# 缝注册表:一个 spec 选实现(离线默认 mock;真实后端 anthropic / openai 各走可选 extra)。
# entry-point group "corespine.llm":第三方 provider 装包即可被发现(与 corespine README 示例同名)。
llm_providers: Registry[LLMProvider] = Registry("llm")
llm_providers.register("mock", lambda **kw: MockProvider(**kw))
llm_providers.register("anthropic", lambda **kw: AnthropicProvider(**kw))
llm_providers.register("openai", lambda **kw: OpenAICompatProvider(**kw))
