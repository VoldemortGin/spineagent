"""最小多 agent 编排:把若干 Agent 顺序 / 并行 / 流水线跑,收集结果。

Coordinator 是 spineagent 的「编排」缝最小实现:零外部依赖、离线可跑(用 mock agent 即可)。
  - run_sequential —— 同一任务逐个跑,保序收集 AgentResult;
  - run_parallel  —— 同一任务用线程池并发跑,结果仍按 agent 顺序返回(确定性 / 可断言);
  - run_pipeline  —— 链式:把上一个 agent 的输出当作下一个 agent 的输入,逐段传递、全程保序。

弹性容错(resilient=True):默认 fail-fast——任一 agent 抛异常即冒泡(与既有行为一致)。开启
resilient 后,单个 agent 的异常被捕获、归一为家族统一错误 dict(corespine.errors.error_to_dict,
含 code / retryable),塞进该步的 AgentResult.error,批次继续(顺序 / 并行跑完其余 agent;流水线
则在失败处停止,因为下游拿不到输入)。一个坏 agent 不再炸穿整批。

隐私:Coordinator 只记【编排级】元数据(模式 / agent 数 / 失败数 / 耗时),绝不记任务/输出正文;
并行分支不向各 agent 共享同一个 sink(避免跨线程写同一列表的竞态),per-agent 的 trace 是 agent
被直接调用时自己的事(见 agent/agent.py 的隐私约定)。
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from corespine.errors import error_to_dict
from corespine.observability.trace import TraceSink

from spineagent.agent.agent import Agent, AgentResult


class Coordinator:
    """顺序 / 并行 / 流水线跑一组 agent,收集结果的最小协调器。"""

    def __init__(self, agents: Iterable[Agent], *, trace: TraceSink | None = None) -> None:
        self._agents = list(agents)
        self._trace = trace

    @property
    def agents(self) -> list[Agent]:
        return list(self._agents)

    def run_sequential(self, task: str, *, resilient: bool = False) -> list[AgentResult]:
        """同一任务逐个跑每个 agent,按顺序收集结果。"""
        start = time.perf_counter()
        results = [self._run_one(agent, task, resilient) for agent in self._agents]
        self._emit("sequential", start, results)
        return results

    def run_parallel(
        self, task: str, *, max_workers: int | None = None, resilient: bool = False
    ) -> list[AgentResult]:
        """同一任务用线程池并发跑;结果仍按 agent 输入顺序返回(map 保序)。"""
        start = time.perf_counter()
        workers = max_workers or max(1, len(self._agents))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda agent: self._run_one(agent, task, resilient), self._agents))
        self._emit("parallel", start, results)
        return results

    def run_pipeline(self, task: str, *, resilient: bool = False) -> list[AgentResult]:
        """链式:上一个 agent 的输出作下一个 agent 的输入,保序收集每段结果。

        resilient 下若某段失败,流水线在该段停止(下游拿不到输入),返回已跑出的各段结果(末段
        带 error);非 resilient 下该段异常照常冒泡。
        """
        start = time.perf_counter()
        results: list[AgentResult] = []
        current = task
        for agent in self._agents:
            result = self._run_one(agent, current, resilient)
            results.append(result)
            if result.error is not None:
                break  # 失败:下游无输入可承接,停在此处。
            current = result.output
        self._emit("pipeline", start, results)
        return results

    def _run_one(self, agent: Agent, task: str, resilient: bool) -> AgentResult:
        """跑一个 agent;resilient 时捕获其异常归一为带 error 的 AgentResult(否则照常冒泡)。"""
        if not resilient:
            return agent.step(task)
        try:
            return agent.step(task)
        except Exception as exc:  # noqa: BLE001 — 弹性模式:任何失败归一为结构化错误,不外抛
            return AgentResult(agent=agent.name, output="", error=error_to_dict(exc))

    def _emit(self, mode: str, start: float, results: list[AgentResult]) -> None:
        """记一条隐私安全的编排级 trace:模式 + agent 数 + 失败数 + 耗时,绝不记正文。"""
        if self._trace is None:
            return
        self._trace.emit(
            "coordinate",
            mode=mode,
            agent_count=len(self._agents),
            failures=sum(1 for r in results if r.error is not None),
            took_ms=round((time.perf_counter() - start) * 1000, 3),
        )
