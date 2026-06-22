"""OpenAI messages/tools → 各家 native 形状的【共享中性中间层】(parse-then-render)。

Anthropic / Gemini / Bedrock 三家适配器把 OpenAI 形状的 messages/tools 转成各自 native 形状时,
【控制流骨架完全一致】(同一套 role 分支:system / tool / assistant+tool_calls / 其余),只有最终
吐出的【block 形状】各家不同。本模块把那段重复的【解析骨架】抽成一个中性中间表示(Turn 序列),
让三家共享同一个 `normalize_openai_messages`,各自只保留「Turn → 自家 block」这段 render。

设计:先 parse(把 OpenAI messages 规整成 frozen dataclass 的 Turn 序列、把 tool 参数 JSON 解码),
后 render(各适配器按 Turn 类型拼自家 block)。本模块【只】依赖标准库,不 import 任何 SDK,也不
反向 import 三家适配器——`_mapping` 被它们依赖,无环。
"""

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCallPart:
    """assistant 发起的一次工具调用(arguments 已 JSON 解码成 dict)。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class SystemTurn:
    """system 角色一条:文本合进各家的 system 参数(由 render 侧汇总)。"""

    text: str


@dataclass(frozen=True)
class ToolResultTurn:
    """tool 角色一条:工具执行结果。

    `tool_call_id` 供 Anthropic / Bedrock 用;`name` 供 Gemini 用(Gemini 的怪癖:它的 tool 分支
    用 `m.get("name", m.get("tool_call_id", ""))` 作 function 名,而非 tool_call_id)。两者都带上,
    各家 render 各取所需,保证逐字等价。
    """

    tool_call_id: str
    content: str
    name: str


@dataclass(frozen=True)
class AssistantToolCallsTurn:
    """assistant 角色且带 tool_calls 的一条:可选前导文本 + 一串工具调用。

    `text` 为 None 时 render 侧【不】吐文本 block(对应原代码 `if content:` 的 falsy 跳过语义:
    None 或空串都不吐)。
    """

    text: str | None
    tool_calls: tuple[ToolCallPart, ...]


@dataclass(frozen=True)
class PlainTurn:
    """普通对话一条:role 已规整为恰好 "user" 或 "assistant"。"""

    role: str
    content: str


Turn = SystemTurn | ToolResultTurn | AssistantToolCallsTurn | PlainTurn


def normalize_openai_messages(messages: list[dict[str, Any]]) -> list[Turn]:
    """OpenAI messages(list[dict])→ 中性 Turn 序列(三家适配器共享的解析骨架)。

    逐条按 role 分支:system → SystemTurn;tool → ToolResultTurn;assistant 且带 tool_calls →
    AssistantToolCallsTurn(falsy content → text=None,工具参数 JSON 解码成 dict);其余 → PlainTurn
    (role 规整成 "assistant"/"user")。
    """
    turns: list[Turn] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            turns.append(SystemTurn(text=str(content or "")))
        elif role == "tool":
            turns.append(
                ToolResultTurn(
                    tool_call_id=m.get("tool_call_id", ""),
                    content=str(content or ""),
                    name=m.get("name", m.get("tool_call_id", "")),
                )
            )
        elif role == "assistant" and m.get("tool_calls"):
            parts: list[ToolCallPart] = []
            for tc in m["tool_calls"]:
                fn = tc["function"]
                parts.append(
                    ToolCallPart(
                        id=tc["id"],
                        name=fn["name"],
                        arguments=json.loads(fn.get("arguments") or "{}"),
                    )
                )
            turns.append(
                AssistantToolCallsTurn(
                    text=(content if content else None),
                    tool_calls=tuple(parts),
                )
            )
        else:
            turns.append(
                PlainTurn(
                    role=("assistant" if role == "assistant" else "user"),
                    content=str(content or ""),
                )
            )
    return turns


def join_system(turns: list[Turn]) -> str:
    """把所有 SystemTurn 的文本用 "\\n" 拼成各家的 system 参数(同原代码合并语义)。"""
    return "\n".join(t.text for t in turns if isinstance(t, SystemTurn))


def unwrap_function_tool(tool: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """OpenAI function-tool 形状 → (name, description, parameters)(三家适配器共享的 tool 拆解)。

    兼容裸 function 体与带 "function" 外壳两种写法;description / parameters 缺省回落与原代码一致。
    """
    fn = tool.get("function", tool)
    name = fn["name"]
    description = fn.get("description", "")
    parameters = fn.get("parameters", {"type": "object", "properties": {}})
    return name, description, parameters
