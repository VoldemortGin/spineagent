"""Google Gemini 适配器:generateContent native → OpenAI ChatCompletion(挂在 corespine LLMProvider 缝)。

Gemini 的形状与 OpenAI 差异最大:contents 用 {role: user/model, parts:[{text}|{function_call}]}、
system 走 system_instruction、tools 是 function_declarations、function_call.args 是 dict 且【无 id】、
finishReason 大写枚举、usage 叫 usageMetadata。本适配器把 OpenAI messages/tools 转成 Gemini 形状,
再把 Gemini 响应转回与 OpenAI 完全一致的 ChatCompletion(自造 tool_call id、args→JSON 串、
finishReason 映射、role model→assistant、usageMetadata 重映射)。reasoning/grounding 本期丢弃。

一个适配器同覆盖 AI Studio 与 Vertex 上的 Gemini(仅 client 构造不同)。import-clean:顶层零 SDK,
未注入 client 时经 lazy_extra_import 拉 [gemini] extra(google-genai)。映射可注入 fake client 离线单测。
"""

from __future__ import annotations

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
        response = self._client.models.generate_content(
            model=self._model, contents=contents, config=config or None
        )
        candidate = response.candidates[0]
        parts = getattr(candidate.content, "parts", None) or []
        text = "".join(p.text for p in parts if getattr(p, "text", None))
        tool_calls = tuple(
            ToolCall(
                id=f"call_{i}_{p.function_call.name}",  # Gemini 无 id,自造稳定 id
                function=FunctionCall(
                    name=p.function_call.name, arguments=json.dumps(dict(p.function_call.args or {}))
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
            choices=(choice,), usage=_gemini_usage(getattr(response, "usage_metadata", None)), model=self._model
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
    fn = tool.get("function", tool)
    return {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "parameters_json_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


def _openai_messages_to_gemini(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI messages → (system_instruction, Gemini contents)。

    system → system_instruction;user → role user;assistant → role model;tool 角色 → function_response
    part;assistant 的 tool_calls → function_call part(让多轮工具结果能喂回 Gemini)。
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            system_parts.append(str(content or ""))
        elif role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": m.get("name", m.get("tool_call_id", "")),
                                "response": {"result": str(content or "")},
                            }
                        }
                    ],
                }
            )
        elif role == "assistant" and m.get("tool_calls"):
            parts: list[dict[str, Any]] = []
            if content:
                parts.append({"text": content})
            for tc in m["tool_calls"]:
                fn = tc["function"]
                parts.append(
                    {"function_call": {"name": fn["name"], "args": json.loads(fn.get("arguments") or "{}")}}
                )
            contents.append({"role": "model", "parts": parts})
        else:
            contents.append(
                {"role": "model" if role == "assistant" else "user", "parts": [{"text": str(content or "")}]}
            )
    return "\n".join(system_parts), contents
