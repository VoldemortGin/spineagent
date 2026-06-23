"""AWS Bedrock 适配器:Converse API native → OpenAI ChatCompletion(挂在 corespine LLMProvider 缝)。

统一走 Bedrock 的 Converse API(跨模型同形:Claude / Llama / Mistral / Cohere / Titan 都一套),
一个适配器覆盖整个 AWS 生态,避免按模型分发。Converse 是 dict 形:output.message.content 是 block
列表({text} 或 {toolUse:{toolUseId,name,input(dict)}})、stopReason 蛇形、usage 用 inputTokens/
outputTokens。本适配器转回与 OpenAI 完全一致的 ChatCompletion(toolUse.input→JSON 串、toolUseId→id、
stopReason 映射、usage 重映射)。

import-clean:顶层零 SDK,未注入 client 时经 lazy_extra_import 拉 [bedrock] extra(boto3);SigV4 由
boto3 处理。映射可注入 fake client 离线单测。
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

_BOTO3_SDK_MODULE = "boto3"

# Bedrock Converse stopReason(蛇形)→ OpenAI finish_reason;未知值落 stop。
_BEDROCK_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
}


def load_boto3_sdk() -> Any:
    """延迟 import 官方 boto3 SDK;未装 [bedrock] extra 时给友好安装指引。"""
    return lazy_extra_import(_BOTO3_SDK_MODULE, pkg="spineagent", extra="bedrock")


class BedrockConverseProvider:
    """走 boto3 bedrock-runtime 的 Converse API 的 LLMProvider 适配器;native → OpenAI 形状。"""

    def __init__(
        self,
        model: str,
        *,
        client: Any = None,
        region_name: str | None = None,
        extra: dict[str, Any] | None = None,
        **client_kwargs: Any,
    ) -> None:
        self._model = model
        self._extra = dict(extra or {})
        if client is None:
            sdk = load_boto3_sdk()  # 仅在未注入 client 时才拉真实 SDK
            client = sdk.client("bedrock-runtime", region_name=region_name, **client_kwargs)
        self._client = client

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        system, convo = _openai_messages_to_bedrock(messages)
        kwargs: dict[str, Any] = dict(self._extra)
        if system:
            kwargs["system"] = [{"text": system}]
        if tools:
            kwargs["toolConfig"] = {"tools": [_openai_tool_to_bedrock(t) for t in tools]}
        # 只包裹 SDK 网络调用:vendor 网络/超时/API 异常归一到 ProviderError;响应映射的程序错
        # 落在 try 外,照常上抛,不被兜底掩盖。
        try:
            response = self._client.converse(modelId=self._model, messages=convo, **kwargs)
        except Exception as exc:  # noqa: BLE001 — SDK 网络/API 异常归一到 ProviderError
            raise ProviderError(f"Bedrock 调用失败:{exc}") from exc
        blocks = response["output"]["message"].get("content", []) or []
        text = "".join(b["text"] for b in blocks if "text" in b)
        tool_calls = tuple(
            ToolCall(
                id=b["toolUse"]["toolUseId"],
                function=FunctionCall(
                    name=b["toolUse"]["name"], arguments=json.dumps(b["toolUse"].get("input", {}))
                ),
            )
            for b in blocks
            if "toolUse" in b
        )
        message = ResponseMessage(
            role="assistant", content=(text or None), tool_calls=(tool_calls or None)
        )
        finish = _BEDROCK_FINISH.get(response.get("stopReason"), "stop")
        choice = Choice(index=0, message=message, finish_reason=finish)
        return ChatCompletion(
            choices=(choice,), usage=_bedrock_usage(response.get("usage")), model=self._model
        )


def _bedrock_usage(usage: dict[str, Any] | None) -> Usage | None:
    if not usage:
        return None
    inp = int(usage.get("inputTokens", 0) or 0)
    out = int(usage.get("outputTokens", 0) or 0)
    return Usage(
        prompt_tokens=inp,
        completion_tokens=out,
        total_tokens=int(usage.get("totalTokens", inp + out)),
    )


def _openai_tool_to_bedrock(tool: dict[str, Any]) -> dict[str, Any]:
    """OpenAI function-tool → Bedrock toolSpec(name/description/inputSchema.json)。"""
    name, description, parameters = unwrap_function_tool(tool)
    return {
        "toolSpec": {
            "name": name,
            "description": description,
            "inputSchema": {"json": parameters},
        }
    }


def _openai_messages_to_bedrock(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI messages → (system 文本, Bedrock Converse messages)。

    共享 `normalize_openai_messages` 解析成中性 Turn 序列,再按 Turn 类型拼 Bedrock block:
    system → system 参数;user/assistant → content[{text}];tool 角色 → toolResult block;
    assistant 的 tool_calls → toolUse block(多轮工具结果可喂回)。
    """
    turns = normalize_openai_messages(messages)
    convo: list[dict[str, Any]] = []
    for turn in turns:
        if isinstance(turn, SystemTurn):
            continue  # system 由 join_system 汇总
        elif isinstance(turn, ToolResultTurn):
            convo.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": turn.tool_call_id,
                                "content": [{"text": turn.content}],
                            }
                        }
                    ],
                }
            )
        elif isinstance(turn, AssistantToolCallsTurn):
            blocks: list[dict[str, Any]] = []
            if turn.text is not None:
                blocks.append({"text": turn.text})
            for p in turn.tool_calls:
                blocks.append(
                    {"toolUse": {"toolUseId": p.id, "name": p.name, "input": p.arguments}}
                )
            convo.append({"role": "assistant", "content": blocks})
        else:
            convo.append({"role": turn.role, "content": [{"text": turn.content}]})
    return join_system(turns), convo
