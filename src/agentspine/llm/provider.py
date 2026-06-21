"""LLM provider 适配器:把真实后端接进 corespine 的 LLMProvider 缝(OpenAI chat-completions 规范)。

家族缝的元模式(同 mcp / a2a):Protocol(在 corespine)+ 离线确定性默认(corespine MockProvider)
+ Registry 工厂 + 真实后端经可选 extra 延迟 import。本模块只提供【两个真实适配器 + 工厂】:

  - AnthropicProvider     —— 走官方 `anthropic` SDK 的 messages.create(默认 claude-opus-4-8);
  - OpenAICompatProvider  —— 走官方 `openai` SDK 的 chat.completions.create + 可配 base_url,
                             一个适配器覆盖 OpenAI 及所有「OpenAI 兼容」端点
                             (Together / Groq / DeepSeek / Ollama / vLLM / Azure …)。

【对外唯一规范 = OpenAI chat completions 形状】用户永远按 OpenAI 规范调用(传 OpenAI 形状的
messages/tools),拿回 OpenAI 形状的 ChatCompletion。OpenAICompatProvider 近 1:1 直传;
AnthropicProvider 在【内部】把 OpenAI messages/tools 转成 Anthropic 原生形状、再把 Anthropic 的
响应(text / tool_use blocks / stop_reason / usage)转回 OpenAI ChatCompletion——用户无感、不 shim。

【import-clean】本模块顶层【绝不】import anthropic / openai;真实 SDK 仅在【未注入 client】且构造
适配器时经 corespine.lazy_extra_import 延迟 import,缺 extra 给「pip install agentspine[…]」友好报错。
映射逻辑可注入 fake client 离线单测,真实路径只是那一行延迟 import。
"""

from __future__ import annotations

import json
from typing import Any

from corespine.llm.provider import (
    ChatCompletion,
    Choice,
    FunctionCall,
    LLMProvider,
    MockProvider,
    ResponseMessage,
    ToolCall,
    Usage,
)
from corespine.seam.registry import Registry, lazy_extra_import

# 非 OpenAI 原生后端适配器(各自顶层零 SDK、延迟 import;import 它们不破 import-clean)。
from agentspine.llm.bedrock_provider import BedrockConverseProvider
from agentspine.llm.cohere_provider import CohereProvider
from agentspine.llm.gemini_provider import GeminiProvider

# 真实 SDK 的 import 名(装了对应 extra 才有);默认离线路径绝不 import 它们。
_ANTHROPIC_SDK_MODULE = "anthropic"
_OPENAI_SDK_MODULE = "openai"

# Anthropic stop_reason → OpenAI finish_reason。
_ANTHROPIC_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def load_anthropic_sdk() -> Any:
    """延迟 import 官方 anthropic SDK;未装 [anthropic] extra 时给友好安装指引。"""
    return lazy_extra_import(_ANTHROPIC_SDK_MODULE, pkg="agentspine", extra="anthropic")


def load_openai_sdk() -> Any:
    """延迟 import 官方 openai SDK;未装 [openai] extra 时给友好安装指引。"""
    return lazy_extra_import(_OPENAI_SDK_MODULE, pkg="agentspine", extra="openai")


class AnthropicProvider:
    """走官方 anthropic SDK 的 LLMProvider 适配器(默认 claude-opus-4-8);内部转 OpenAI 形状吐出。

    构造:未注入 client 时延迟 import anthropic 并建 `Anthropic(**client_kwargs)`(api_key 默认
    从环境 ANTHROPIC_API_KEY 读)。`extra` 透传给 messages.create(如需 thinking / 流式等自行加)。
    chat:OpenAI messages → Anthropic(system 单独、tool 角色转 tool_result、assistant 的 tool_calls
    转 tool_use);OpenAI function-tool → Anthropic input_schema;再把 Anthropic 响应转回 OpenAI
    ChatCompletion(text block → message.content、tool_use → tool_calls(arguments JSON 串)、
    stop_reason → finish_reason、usage → prompt/completion_tokens)。
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

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        system, convo = _openai_messages_to_anthropic(messages)
        kwargs = dict(self._extra)
        if tools:
            kwargs["tools"] = [_openai_tool_to_anthropic(t) for t in tools]
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=convo,
            **kwargs,
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        tool_calls = tuple(
            ToolCall(id=b.id, function=FunctionCall(name=b.name, arguments=json.dumps(b.input)))
            for b in response.content
            if getattr(b, "type", None) == "tool_use"
        )
        message = ResponseMessage(
            role="assistant", content=(text or None), tool_calls=(tool_calls or None)
        )
        choice = Choice(
            index=0,
            message=message,
            finish_reason=_ANTHROPIC_FINISH.get(response.stop_reason, "stop"),
        )
        usage = Usage(
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
        )
        return ChatCompletion(
            choices=(choice,),
            usage=usage,
            model=getattr(response, "model", self._model),
            id=getattr(response, "id", ""),
        )


class OpenAICompatProvider:
    """走官方 openai SDK 的 LLMProvider 适配器,可配 base_url 覆盖一切「OpenAI 兼容」端点。

    model 必填(各兼容端点的模型名不同,无通用默认)。base_url 指向兼容端点(留空即官方 OpenAI)。
    chat:messages / tools 直传(本就是 OpenAI 形状),把 SDK 的 OpenAI 响应对象规整成本家 dataclass
    形状的 ChatCompletion(字段一一对应,arguments 原样保留 JSON 串)。
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

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        kwargs = dict(self._extra)
        if tools:
            kwargs["tools"] = tools  # 已是 OpenAI function-tool 形状,直传
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=self._max_tokens,
            **kwargs,
        )
        choices = tuple(
            Choice(
                index=getattr(c, "index", i),
                finish_reason=getattr(c, "finish_reason", None) or "stop",
                message=ResponseMessage(
                    role=c.message.role,
                    content=c.message.content,
                    tool_calls=tuple(
                        ToolCall(
                            id=tc.id,
                            function=FunctionCall(
                                name=tc.function.name, arguments=tc.function.arguments or "{}"
                            ),
                        )
                        for tc in (getattr(c.message, "tool_calls", None) or [])
                    )
                    or None,
                ),
            )
            for i, c in enumerate(response.choices)
        )
        u = getattr(response, "usage", None)
        usage = (
            Usage(
                prompt_tokens=u.prompt_tokens,
                completion_tokens=u.completion_tokens,
                total_tokens=getattr(u, "total_tokens", u.prompt_tokens + u.completion_tokens),
            )
            if u
            else None
        )
        return ChatCompletion(
            choices=choices,
            usage=usage,
            model=getattr(response, "model", self._model),
            id=getattr(response, "id", ""),
            created=getattr(response, "created", 0),
            object=getattr(response, "object", "chat.completion"),
        )


def _openai_tool_to_anthropic(tool: dict[str, Any]) -> dict[str, Any]:
    """OpenAI function-tool 形状 → Anthropic 工具形状(name/description/input_schema)。"""
    fn = tool.get("function", tool)
    return {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


def _openai_messages_to_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI messages(list[dict])→ (system 字符串, Anthropic messages)。

    system 角色合并进 system 参数;tool 角色转 tool_result block;assistant 的 tool_calls 转
    tool_use block——让多轮 function-calling 的工具结果能原样喂回 Anthropic。
    """
    system_parts: list[str] = []
    convo: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            system_parts.append(str(content or ""))
        elif role == "tool":
            convo.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.get("tool_call_id", ""),
                            "content": str(content or ""),
                        }
                    ],
                }
            )
        elif role == "assistant" and m.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in m["tool_calls"]:
                fn = tc["function"]
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": fn["name"],
                        "input": json.loads(fn.get("arguments") or "{}"),
                    }
                )
            convo.append({"role": "assistant", "content": blocks})
        else:
            convo.append(
                {"role": "assistant" if role == "assistant" else "user", "content": str(content or "")}
            )
    return "\n".join(system_parts), convo


# 缝注册表:一个 spec 选实现(离线默认 mock;真实后端各走可选 extra 延迟 import)。
# 非 OpenAI 原生后端(anthropic / cohere / …)的适配器把 native 响应转成 OpenAI ChatCompletion;
# OpenAI 兼容的一律走 openai。entry-point group "corespine.llm":第三方 provider 装包即可被发现。
llm_providers: Registry[LLMProvider] = Registry("llm")
llm_providers.register("mock", lambda **kw: MockProvider(**kw))
llm_providers.register("anthropic", lambda **kw: AnthropicProvider(**kw))
llm_providers.register("openai", lambda **kw: OpenAICompatProvider(**kw))
llm_providers.register("cohere", lambda **kw: CohereProvider(**kw))
llm_providers.register("gemini", lambda **kw: GeminiProvider(**kw))
llm_providers.register("bedrock", lambda **kw: BedrockConverseProvider(**kw))
