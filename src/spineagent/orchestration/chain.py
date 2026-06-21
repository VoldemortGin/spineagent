"""把一串 agent 串成单个 Agent:逐段把上一个的输出喂给下一个,返回末端结果。

ChainAgent 让「流水线」成为一等【可组合单元】:它本身实现 Agent 协议,故可进 Coordinator 顺序 /
并行编排、被 AgentTool 当工具调用、或作为另一个 chain 的一环——chains of chains。step 内复用
Coordinator 的流水线把任务逐段传递(DRY,不另造一套链式逻辑),返回末端 agent 的输出(provenance
= chain 名)。失败照常冒泡(fail-fast,与 Agent 协议「成功产出非空、失败抛异常」一致)。

放在 orchestration 层而非 agent 层:它是「把若干 agent 编排成一个 agent」的编排构件,依赖 agent
(方向 orchestration→agent,与 Coordinator 一致),不反向。

隐私:只发一条 chain 级 trace(chain 名 / 跑过的段数 / 输出长度),内部流水线不向子 agent 透
trace(与 Coordinator 一致),绝不记任务 / 输出正文。
"""

from __future__ import annotations

from collections.abc import Iterable

from corespine.observability.trace import TraceSink

from spineagent.agent.agent import Agent, AgentResult
from spineagent.orchestration.coordinator import Coordinator


class ChainAgent:
    """把一串 agent 串成单个 Agent(实现 Agent 协议),让流水线成为一等可组合单元。"""

    def __init__(self, name: str, agents: Iterable[Agent]) -> None:
        self._name = name
        self._agents = list(agents)

    @property
    def name(self) -> str:
        return self._name

    def step(self, task: str, *, trace: TraceSink | None = None) -> AgentResult:
        # 复用 Coordinator 流水线;内部不透 trace,失败在此冒泡(fail-fast)。
        results = Coordinator(self._agents).run_pipeline(task)
        # 末端 agent 的输出即 chain 的产出;空链(无 agent)退化为恒等透传。
        output = results[-1].output if results else task
        _emit_chain_step(trace, self._name, len(results), output)
        return AgentResult(agent=self._name, output=output)


def _emit_chain_step(trace: TraceSink | None, name: str, stages: int, output: str) -> None:
    """记一条隐私安全的 chain 级 trace:chain 名 / 跑过的段数 / 输出长度,绝不记正文。"""
    if trace is None:
        return
    trace.emit("chain_step", agent=name, stages=stages, output_chars=len(output))
