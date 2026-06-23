"""Cohere v2 chat 适配器:Cohere native → OpenAI ChatCompletion(挂在 corespine LLMProvider 缝)。

Cohere v2(ClientV2.chat)结构已很接近 OpenAI:tools 就是 OpenAI function-tool 形状、tool_calls 的
function.arguments 本就是 JSON 串。差异只在【响应侧】:finish_reason 大写(COMPLETE / TOOL_CALL /
MAX_TOKENS …)、content 是 block 数组(取 text block)、usage 层级不同(usage.tokens / billed_units)。
本适配器把这些转回与 OpenAI 完全一致的 ChatCompletion;reasoning / citations 本期丢弃(同 Anthropic)。

import-clean:顶层【绝不】import cohere;真实 SDK 仅在未注入 client 时经 lazy_extra_import 延迟拉取
([cohere] extra),缺则「pip install spineagent[cohere]」友好报错。映射可注入 fake client 离线单测。
"""

from typing import Any

from corespine.llm.provider import (
    ChatCompletion,
    Choice,
    FunctionCall,
    ResponseMessage,
    ToolCall,
    Usage,
)
from corespine.seam.registry import lazy_extra_import

from spineagent.llm.errors import ProviderError

_COHERE_SDK_MODULE = "cohere"

# Cohere finish_reason(大写)→ OpenAI finish_reason;未知值一律落 stop。
_COHERE_FINISH = {
    "COMPLETE": "stop",
    "STOP_SEQUENCE": "stop",
    "TOOL_CALL": "tool_calls",
    "MAX_TOKENS": "length",
    "ERROR": "stop",
}


def load_cohere_sdk() -> Any:
    """延迟 import 官方 cohere SDK;未装 [cohere] extra 时给友好安装指引。"""
    return lazy_extra_import(_COHERE_SDK_MODULE, pkg="spineagent", extra="cohere")


class CohereProvider:
    """走官方 cohere SDK(ClientV2.chat)的 LLMProvider 适配器;native → OpenAI 形状吐出。"""

    def __init__(
        self,
        *,
        model: str = "command-r-plus",
        client: Any = None,
        extra: dict[str, Any] | None = None,
        **client_kwargs: Any,
    ) -> None:
        self._model = model
        self._extra = dict(extra or {})
        if client is None:
            sdk = load_cohere_sdk()  # 仅在未注入 client 时才拉真实 SDK
            client = sdk.ClientV2(**client_kwargs)
        self._client = client

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        kwargs = dict(self._extra)
        if tools:
            kwargs["tools"] = tools  # Cohere v2 tools 已是 OpenAI function-tool 形状,直传
        # 只包裹 SDK 网络调用:vendor 网络/超时/API 异常归一到 ProviderError;响应映射的程序错
        # 落在 try 外,照常上抛,不被兜底掩盖。
        try:
            response = self._client.chat(model=self._model, messages=messages, **kwargs)
        except Exception as exc:  # noqa: BLE001 — SDK 网络/API 异常归一到 ProviderError
            raise ProviderError(f"Cohere 调用失败:{exc}") from exc
        message = response.message
        text = "".join(
            b.text
            for b in (getattr(message, "content", None) or [])
            if getattr(b, "type", None) == "text"
        )
        tool_calls = tuple(
            ToolCall(
                id=tc.id,
                function=FunctionCall(
                    name=tc.function.name, arguments=tc.function.arguments or "{}"
                ),
            )
            for tc in (getattr(message, "tool_calls", None) or [])
        )
        result_message = ResponseMessage(
            role="assistant", content=(text or None), tool_calls=(tool_calls or None)
        )
        finish = _COHERE_FINISH.get(str(getattr(response, "finish_reason", None) or ""), "stop")
        choice = Choice(index=0, message=result_message, finish_reason=finish)
        return ChatCompletion(
            choices=(choice,),
            usage=_cohere_usage(getattr(response, "usage", None)),
            model=self._model,
        )


def _cohere_usage(usage: Any) -> Usage | None:
    """Cohere usage(usage.tokens 或 usage.billed_units 的 input/output_tokens)→ OpenAI Usage。"""
    if usage is None:
        return None
    tokens = getattr(usage, "tokens", None) or getattr(usage, "billed_units", None)
    if tokens is None:
        return None
    inp = int(getattr(tokens, "input_tokens", 0) or 0)
    out = int(getattr(tokens, "output_tokens", 0) or 0)
    return Usage(prompt_tokens=inp, completion_tokens=out, total_tokens=inp + out)
