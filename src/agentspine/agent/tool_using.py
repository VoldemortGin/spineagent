"""会用工具的多步 agent:在一次 step() 内跑「决策→调工具→喂回观测→再决策」的循环。

ToolUsingAgent 实现现有 Agent 协议(name + step),因此【零改 Coordinator】即可进顺序 / 并行
编排。一次 step() 内:用一个 ToolPolicy 决定下一个动作——调某工具则按名取 Tool 执行、把观测
追加进历史(供下一步 $prev 链式消费)、发一条隐私安全步级 trace;收尾则返回最终 AgentResult。

max_steps 守卫:其语义是【最多调用多少次工具】(收尾决策本身不占步预算)。已用满 max_steps
次工具调用后,policy 若还想再调工具,则强制收尾——即便 policy 异常永不返回 Finish,也绝不
死循环(history 每步单调增长,必在 max_steps 内触顶)。

$prev:工具参数里字面量 `$prev` 在【执行前】替换为上一步观测的输出(history 为空时替换为空
串),让「把观测喂回循环」名副其实。替换只发生在内存里,trace 只记其长度,绝不写进正文。
注意:若【首步】即引用 $prev(尚无上一步输出),替换为空串后,余下参数能否被工具处理由工具
自身决定;工具若拒绝该参数(如 CalcTool 对空串抛错),异常照常上抛——错误处理 / 重试不在本
增量范围(rule of three),调用方自行处理。

隐私 trace:每步发 tool_step(agent / 步序 / 工具名 / 入参长度 / 输出长度),收尾发 agent_finish
(agent / 总步数 / 答案长度),触顶发 agent_step_limit——字段全为 code / 计数 / 序号,键名全程
规避 corespine FORBIDDEN_KEYS,绝不携带 task / arg / output / answer 正文。
"""

from __future__ import annotations

from collections.abc import Iterable

from corespine.observability.trace import TraceSink

from agentspine.agent.agent import AgentResult
from agentspine.agent.policy import Finish, Observation, ToolPolicy
from agentspine.tools.tool import Tool

# 触顶 max_steps 又无任何观测可作答时的固定兜底文案(保证产出非空)。
_NO_OUTPUT = "(reached max_steps without finishing)"


class ToolUsingAgent:
    """用一个 ToolPolicy 驱动、在单次 step() 内多步调用工具的 agent(实现 Agent 协议)。"""

    def __init__(
        self,
        name: str,
        policy: ToolPolicy,
        tools: Iterable[Tool],
        *,
        max_steps: int = 8,
    ) -> None:
        self._name = name
        self._policy = policy
        self._tools = {tool.name: tool for tool in tools}
        # 工具名集合(传给 policy 用以避免幻觉一个不存在的工具);顺序 = 插入序。
        self._tool_names = tuple(self._tools)
        self._max_steps = max_steps

    @property
    def name(self) -> str:
        return self._name

    def step(self, task: str, *, trace: TraceSink | None = None) -> AgentResult:
        history: list[Observation] = []
        while True:
            action = self._policy.decide(
                task, tools=self._tool_names, history=tuple(history)
            )
            if isinstance(action, Finish):
                _emit_finish(trace, self._name, len(history), action.answer)
                return AgentResult(agent=self._name, output=action.answer)
            # ToolCall:已用满 max_steps 次工具调用,policy 还想再调 -> 触顶强制收尾。
            if len(history) >= self._max_steps:
                answer = (history[-1].output if history else "") or _NO_OUTPUT
                _emit_step_limit(trace, self._name, self._max_steps)
                _emit_finish(trace, self._name, len(history), answer)
                return AgentResult(agent=self._name, output=answer)
            # 把 $prev 替换为上一步观测输出后执行该工具,观测追加进历史。
            arg = action.arg.replace("$prev", history[-1].output if history else "")
            result = self._tools[action.tool].run(arg)
            history.append(Observation(tool=action.tool, arg=arg, output=result.output))
            _emit_tool_step(trace, self._name, len(history) - 1, action.tool, arg, result.output)


def _emit_tool_step(
    trace: TraceSink | None, name: str, step: int, tool: str, arg: str, output: str
) -> None:
    """记一条隐私安全的步级 trace:只记 agent 名 / 步序 / 工具名 / 入参与输出长度,绝不记正文。"""
    if trace is None:
        return
    trace.emit(
        "tool_step",
        agent=name,
        step=step,
        tool=tool,
        arg_chars=len(arg),
        output_chars=len(output),
    )


def _emit_finish(trace: TraceSink | None, name: str, steps: int, answer: str) -> None:
    """记收尾 trace:agent 名 / 总步数 / 答案长度(answer_chars,绝不用 answer 键携带正文)。"""
    if trace is None:
        return
    trace.emit("agent_finish", agent=name, steps=steps, answer_chars=len(answer))


def _emit_step_limit(trace: TraceSink | None, name: str, max_steps: int) -> None:
    """记触顶 trace:便于排障「为何提前收尾」。"""
    if trace is None:
        return
    trace.emit("agent_step_limit", agent=name, max_steps=max_steps)
