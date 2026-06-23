"""Google Gemini 适配器:generateContent native → OpenAI ChatCompletion(挂在 corespine LLMProvider 缝)。

Gemini 的形状与 OpenAI 差异最大:contents 用 {role: user/model, parts:[{text}|{function_call}]}、
system 走 system_instruction、tools 是 function_declarations、function_call.args 是 dict 且【无 id】、
finishReason 大写枚举、usage 叫 usageMetadata。本适配器把 OpenAI messages/tools 转成 Gemini 形状,
再把 Gemini 响应转回与 OpenAI 完全一致的 ChatCompletion(自造 tool_call id、args→JSON 串、
finishReason 映射、role model→assistant、usageMetadata 重映射)。reasoning/grounding 本期丢弃。

一个适配器同覆盖 AI Studio 与 Vertex 上的 Gemini(仅 client 构造不同)。import-clean:顶层零 SDK,
未注入 client 时经 lazy_extra_import 拉 [gemini] extra(google-genai)。映射可注入 fake client 离线单测。
"""

import json
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

from spineagent.llm._mapping import (
    AssistantToolCallsTurn,
    SystemTurn,
    ToolResultTurn,
    join_system,
    normalize_openai_messages,
    unwrap_function_tool,
)
from spineagent.llm.errors import ProviderError

_GEMINI_SDK_MODULE = "google.genai"

# Gemini finishReason(大写枚举)→ OpenAI finish_reason;未知值落 stop。
_GEMINI_FINISH = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "BLOCKLIST": "content_filter",
}


def load_gemini_sdk() -> Any:
    """延迟 import 官方 google-genai SDK;未装 [gemini] extra 时给友好安装指引。"""
    return lazy_extra_import(_GEMINI_SDK_MODULE, pkg="spineagent", extra="gemini")


class GeminiProvider:
    """走官方 google-genai SDK(models.generate_content)的 LLMProvider 适配器;native → OpenAI 形状。"""

    def __init__(
        self,
        *,
        model: str = "gemini-2.5-flash",
        client: Any = None,
        extra: dict[str, Any] | None = None,
        **client_kwargs: Any,
    ) -> None:
        self._model = model
        self._extra = dict(extra or {})
        if client is None:
            sdk = load_gemini_sdk()  # 仅在未注入 client 时才拉真实 SDK
            client = sdk.Client(**client_kwargs)
        self._client = client

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        system, contents = _openai_messages_to_gemini(messages)
        config: dict[str, Any] = dict(self._extra.get("config", {}))
        if system:
            config["system_instruction"] = system
        if tools:
            config["tools"] = [
                {"function_declarations": [_openai_tool_to_gemini(t) for t in tools]}
            ]
        # 只包裹 SDK 网络调用:vendor 网络/超时/API 异常归一到 ProviderError;响应映射的程序错
        # 落在 try 外,照常上抛,不被兜底掩盖。
        try:
            response = self._client.models.generate_content(
                model=self._model, contents=contents, config=config or None
            )
        except Exception as exc:  # noqa: BLE001 — SDK 网络/API 异常归一到 ProviderError
            raise ProviderError(f"Gemini 调用失败:{exc}") from exc
        candidate = response.candidates[0]
        parts = getattr(candidate.content, "parts", None) or []
        text = "".join(p.text for p in parts if getattr(p, "text", None))
        tool_calls = tuple(
            ToolCall(
                id=f"call_{i}_{p.function_call.name}",  # Gemini 无 id,自造稳定 id
                function=FunctionCall(
                    name=p.function_call.name,
                    arguments=json.dumps(dict(p.function_call.args or {})),
                ),
            )
            for i, p in enumerate(parts)
            if getattr(p, "function_call", None)
        )
        finish = _gemini_finish(getattr(candidate, "finish_reason", None))
        if tool_calls:  # 有工具调用时 OpenAI 语义是 tool_calls
            finish = "tool_calls"
        message = ResponseMessage(
            role="assistant", content=(text or None), tool_calls=(tool_calls or None)
        )
        choice = Choice(index=0, message=message, finish_reason=finish)
        return ChatCompletion(
            choices=(choice,),
            usage=_gemini_usage(getattr(response, "usage_metadata", None)),
            model=self._model,
        )


def _gemini_finish(finish_reason: Any) -> str:
    # finish_reason 可能是枚举(有 .name)或字符串。
    key = getattr(finish_reason, "name", None) or str(finish_reason or "")
    return _GEMINI_FINISH.get(key, "stop")


def _gemini_usage(meta: Any) -> Usage | None:
    if meta is None:
        return None
    inp = int(getattr(meta, "prompt_token_count", 0) or 0)
    out = int(getattr(meta, "candidates_token_count", 0) or 0)
    total = int(getattr(meta, "total_token_count", 0) or 0) or (inp + out)
    return Usage(prompt_tokens=inp, completion_tokens=out, total_tokens=total)


def _openai_tool_to_gemini(tool: dict[str, Any]) -> dict[str, Any]:
    """OpenAI function-tool → Gemini FunctionDeclaration。

    注:用 parameters_json_schema(承载标准 JSON Schema,与 OpenAI tool 的 parameters 同源)而非
    FunctionDeclaration.parameters(后者是 Gemini 自家 Schema 类型,不是 JSON Schema)。
    """
    name, description, parameters = unwrap_function_tool(tool)
    return {
        "name": name,
        "description": description,
        "parameters_json_schema": parameters,
    }


def _openai_messages_to_gemini(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI messages → (system_instruction, Gemini contents)。

    共享 `normalize_openai_messages` 解析成中性 Turn 序列,再按 Turn 类型拼 Gemini part:
    system → system_instruction;user → role user;assistant → role model;tool 角色 → function_response
    part(用 turn.name——Gemini 的怪癖,见 _mapping);assistant 的 tool_calls → function_call part。
    """
    turns = normalize_openai_messages(messages)
    contents: list[dict[str, Any]] = []
    for turn in turns:
        if isinstance(turn, SystemTurn):
            continue  # system 由 join_system 汇总进 system_instruction
        elif isinstance(turn, ToolResultTurn):
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": turn.name,
                                "response": {"result": turn.content},
                            }
                        }
                    ],
                }
            )
        elif isinstance(turn, AssistantToolCallsTurn):
            parts: list[dict[str, Any]] = []
            if turn.text is not None:
                parts.append({"text": turn.text})
            for p in turn.tool_calls:
                parts.append({"function_call": {"name": p.name, "args": p.arguments}})
            contents.append({"role": "model", "parts": parts})
        else:
            contents.append(
                {
                    "role": ("model" if turn.role == "assistant" else "user"),
                    "parts": [{"text": turn.content}],
                }
            )
    return join_system(turns), contents
