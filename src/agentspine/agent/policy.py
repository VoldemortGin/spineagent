"""tool-policy 缝:ToolPolicy 协议 + 离线确定性默认(决定 agent 下一步调哪个工具)。

家族缝的元模式(同 mcp / a2a 缝):Protocol + 离线确定性默认 + Registry 工厂 + 参数化
conformance。一个 ToolPolicy 是「会用工具的 agent」的大脑:给一个任务、当前可用的工具名
集合、以及已执行步的观测历史,定下一个动作——【调某个工具】或【收尾给最终答案】。

【离线默认为何不靠 LLM 推理(诚实性取舍)】corespine 的 MockProvider 只做「回声 + sha256
指纹」,对任何 prompt 都吐 `[mock:<hex>] <prompt>`,绝不可能推理出 `{tool: calc, arg: 1+1}`。
任何「解析 LLM 输出找 tool call」的默认实现,在离线下要么永远解析失败、空转到 max_steps,
要么把 mock 输出硬塞进解析器自欺。所以离线默认【绝不依赖 LLM 选工具】——正如 MockProvider
用回声诚实地「不假装生成内容」,SyntaxToolPolicy 把「工具调用意图」显式化为任务文本里的
确定性语法,纯函数解析。它是这条缝的【确定性参照实现】;真实推理式 policy(走 corespine
真 provider 解析 function-calling)应注册进 tool_policies 的 'llm' 位,与 mcp/a2a 的
offline/real 二分完全同构。

decide 是【无状态纯函数】:循环状态全由入参 history 携带,同一 (task, tools, history) 恒定
产出同一 Action——可断言、可复现、零网络,也便于将来直接映射到一次 stateless completion。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, runtime_checkable

from corespine.errors import SeamError
from corespine.seam.registry import Registry


@dataclass(frozen=True)
class ToolCall:
    """决定:调一个工具(tool 名 + 传给它的参数)。arg 中字面量 $prev 由 agent 侧替换为上一步观测。"""

    tool: str
    arg: str


@dataclass(frozen=True)
class Finish:
    """决定:收尾,给出最终答案(保证非空)。"""

    answer: str


# 一个决策动作:调工具,或收尾。isinstance 分发(PEP 604 联合)。
Action: TypeAlias = ToolCall | Finish


@dataclass(frozen=True)
class Observation:
    """一步执行的观测:产出它的工具名 + 实际入参 + 输出。喂回循环,供 $prev 链式消费与收尾拼接。"""

    tool: str
    arg: str
    output: str


@runtime_checkable
class ToolPolicy(Protocol):
    """tool-policy 协议:给 task + 可用工具名集 + 历史观测,决定下一个动作(调工具 / 收尾)。"""

    def decide(
        self, task: str, *, tools: tuple[str, ...], history: tuple[Observation, ...]
    ) -> Action: ...


def _parse_instruction(line: str, tools: tuple[str, ...]) -> tuple[str, str] | None:
    """把一行解析成 (工具名, 参数):形如 `<tool>: <arg>` 且工具名在 tools 内才算指令,否则视为正文。"""
    if ":" not in line:
        return None
    name, _, arg = line.partition(":")
    name = name.strip()
    if name in tools:
        return name, arg.strip()
    return None


class SyntaxToolPolicy:
    """离线确定性默认:按任务文本里的 `<tool>: <arg>` 显式语法 + 工具名集合,确定性路由工具调用。

    无状态纯函数:游标 = len(history) 表示「已执行到第几条工具指令」。第 cursor 条工具指令尚存
    则返回 ToolCall(该行工具名, 该行参数);工具指令耗尽则返回 Finish(把非指令正文行 + 最后一步
    观测按固定模板拼成最终答案,保证非空)。同一输入恒定同一输出。
    """

    def decide(
        self, task: str, *, tools: tuple[str, ...], history: tuple[Observation, ...]
    ) -> Action:
        # 单遍把每行分流:能解析成工具指令的入 instructions,其余非空行入 prose 正文。
        instructions: list[tuple[str, str]] = []
        prose: list[str] = []
        for line in task.splitlines():
            parsed = _parse_instruction(line, tools)
            if parsed is not None:
                instructions.append(parsed)
            elif stripped := line.strip():
                prose.append(stripped)
        cursor = len(history)
        if cursor < len(instructions):
            name, arg = instructions[cursor]
            return ToolCall(tool=name, arg=arg)
        # 工具指令耗尽 -> 收尾:非指令正文行 + 最后一步观测输出,拼成非空答案。
        parts = prose + ([history[-1].output] if history else [])
        answer = "\n".join(p for p in parts if p) or task.strip() or "(no output)"
        return Finish(answer=answer)


def _make_llm_policy(**kwargs: Any) -> ToolPolicy:
    # 真实推理式 policy 的占位(与 mcp/a2a 的 'real' 同构)。不同点:LLM policy 走 corespine
    # LLMProvider 真后端,无网络 SDK 可延迟 import,故直接给「缝未接入」的明确报错。
    # 用家族统一 SeamError(code="seam.unknown"):任何「缝槽存在但真实实现未接入」都同一形状。
    raise SeamError(
        "真实 LLM 推理式 ToolPolicy 留待接入:应走 corespine LLMProvider 真后端解析 "
        "function-calling / 工具调用,并注册进 tool_policies 的 'llm' 位;"
        "本壳只提供缝 + 离线确定性默认 SyntaxToolPolicy。"
    )


# 缝注册表:一个 spec 选实现(默认 offline 离线确定性默认;llm 走真实推理式接入)。
tool_policies: Registry[ToolPolicy] = Registry("tool_policy")
tool_policies.register("offline", lambda **kw: SyntaxToolPolicy(**kw))
tool_policies.register("llm", _make_llm_policy)
