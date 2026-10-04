"""最小多 agent 编排:把若干 Agent 顺序 / 并行 / 流水线跑,收集结果。

Coordinator 是 spineagent 的「编排」缝最小实现:零外部依赖、离线可跑(用 mock agent 即可)。
  - run_sequential —— 同一任务逐个跑,保序收集 AgentResult;
  - run_parallel  —— 同一任务用线程池并发跑,结果仍按 agent 顺序返回(确定性 / 可断言);
  - run_pipeline  —— 链式:把上一个 agent 的输出当作下一个 agent 的输入,逐段传递、全程保序。
                     上游输出以【数据】身份传给下游(agent/trust.py 的 untrusted):下游可读可转述,
                     但指令语法解析器绝不把它当工具调用执行(见 docs/adr/0003)。

弹性容错(resilient=True):默认 fail-fast——任一 agent 抛异常即冒泡(与既有行为一致)。开启
resilient 后,单个 agent 的异常被捕获、归一为家族统一错误 dict(corespine.errors.error_to_dict,
含 code / retryable),塞进该步的 AgentResult.error,批次继续(顺序 / 并行跑完其余 agent;流水线
则在失败处停止,因为下游拿不到输入)。一个坏 agent 不再炸穿整批。

隐私:Coordinator 只记【编排级】元数据(模式 / agent 数 / 失败数 / 耗时),绝不记任务/输出正文;
并行分支不向各 agent 共享同一个 sink(避免跨线程写同一列表的竞态),per-agent 的 trace 是 agent
被直接调用时自己的事(见 agent/agent.py 的隐私约定)。
"""

import contextvars
import math
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

from corespine.errors import CorespineError, error_to_dict
from corespine.observability.trace import TraceSink

from spineagent.agent.agent import Agent, AgentResult
from spineagent.agent.trust import untrusted


def bind_context[R](fn: Callable[..., R]) -> Callable[..., R]:
    """把【当前】contextvars 上下文绑到 fn 上,返回可交给自建线程 / 线程池执行的可调用对象。

    `contextvars.copy_context().run` 的薄封装:在调用 bind_context 的那一刻复制上下文(审批作用域、
    审批记账、approval_scope 等都在里面),之后无论在哪条线程里调用,fn 都在这份副本里跑。用法:
    `pool.submit(bind_context(agent.step), task)`。线程池缺省【不】传播上下文——不包这一层,
    ApprovalMiddleware 的动态作用域就到不了自建线程里的执行点。安全场景仍首选
    require_approval(把闸静态绑在工具对象上,不依赖任何上下文)。
    """
    return _ContextBound(contextvars.copy_context(), fn)


class _ContextBound[R]:
    """bind_context 的返回值:每次调用都在绑定时复制的上下文副本里执行(各次调用各用一份副本)。"""

    def __init__(self, context: contextvars.Context, fn: Callable[..., R]) -> None:
        self._context = context
        self._fn = fn

    def __call__(self, *args: Any, **kwargs: Any) -> R:
        return self._context.copy().run(self._fn, *args, **kwargs)


class AgentTimeoutError(CorespineError):
    """并行编排里某个 agent 超过总超时 / 单任务超时仍未返回(可重试:挂死常是瞬时的)。"""

    code = "orchestration.timeout"
    retryable = True


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
        self,
        task: str,
        *,
        max_workers: int | None = None,
        resilient: bool = False,
        timeout: float | None = None,
        task_timeout: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> list[AgentResult]:
        """同一任务用线程池并发跑;结果仍按 agent 输入顺序返回(map 保序)。

        timeout:整批总超时(秒,从调用起算);task_timeout:单任务超时(秒,从该任务真正开始跑起算)。
        二者缺省 None = 不限(与旧行为完全一致)。超时的任务不再卡住整批:无论 resilient 与否,都以
        error.code = "orchestration.timeout" 的 AgentResult 返回;已完成的照常返回。clock 可注入
        (默认 time.monotonic)以便离线确定性测试。注:Python 线程无法被强杀,挂死的 agent 仍在后台
        线程里占着,直到它自己返回。
        """
        start = time.perf_counter()
        workers = max_workers or max(1, len(self._agents))
        # 每个分支在调用方上下文的一份副本里跑:contextvar 承载的审批作用域等随之进入工作线程,
        # 并行编排下执行闸照样生效(线程池默认不传播上下文)。
        contexts = [contextvars.copy_context() for _ in self._agents]
        if timeout is None and task_timeout is None:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(
                    pool.map(
                        lambda pair: pair[0].run(self._run_one, pair[1], task, resilient),
                        zip(contexts, self._agents, strict=True),
                    )
                )
        else:
            results = self._run_parallel_bounded(
                task, workers, contexts, resilient, timeout, task_timeout, clock
            )
        self._emit("parallel", start, results)
        return results

    def _run_parallel_bounded(
        self,
        task: str,
        workers: int,
        contexts: list[contextvars.Context],
        resilient: bool,
        timeout: float | None,
        task_timeout: float | None,
        clock: Callable[[], float],
    ) -> list[AgentResult]:
        """带总超时 / 单任务超时的并行跑:到点仍未完成的任务以类型化超时结果返回,绝不挂住整批。"""
        began = clock()
        total_deadline = math.inf if timeout is None else began + timeout
        started: dict[int, float] = {}  # 任务序 -> 真正开始跑的时刻(单任务超时由此起算)
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures: list[Future[AgentResult]] = [
                pool.submit(
                    ctx.run, self._run_tracked, index, started, clock, agent, task, resilient
                )
                for index, (ctx, agent) in enumerate(zip(contexts, self._agents, strict=True))
            ]
            timed_out: set[int] = set()
            pending = set(range(len(futures)))
            while pending:
                now = clock()
                for index in list(pending):
                    task_deadline = (
                        started[index] + task_timeout
                        if task_timeout is not None and index in started
                        else math.inf
                    )
                    if futures[index].done():
                        pending.discard(index)
                    elif now >= min(total_deadline, task_deadline):
                        timed_out.add(index)
                        pending.discard(index)
                if not pending:
                    break
                horizon = min(
                    [total_deadline]
                    + [
                        started[i] + task_timeout
                        for i in pending
                        if task_timeout is not None and i in started
                    ]
                )
                # 未开始的任务没有单任务 deadline;无总超时时以短间隔轮询它们何时开始。
                wait_for = 0.05 if math.isinf(horizon) else max(0.0, horizon - now)
                wait([futures[i] for i in pending], timeout=wait_for, return_when=FIRST_COMPLETED)
            results: list[AgentResult] = []
            for index, (agent, future) in enumerate(zip(self._agents, futures, strict=True)):
                if index in timed_out:
                    future.cancel()
                    results.append(_timeout_result(agent, timeout, task_timeout))
                else:
                    results.append(future.result())  # 非 resilient 下的异常照常冒泡
            return results
        finally:
            # 不等挂死的线程:取消尚未开始的任务后立即返回。
            pool.shutdown(wait=False, cancel_futures=True)

    def _run_tracked(
        self,
        index: int,
        started: dict[int, float],
        clock: Callable[[], float],
        agent: Agent,
        task: str,
        resilient: bool,
    ) -> AgentResult:
        started[index] = clock()
        return self._run_one(agent, task, resilient)

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
            current = untrusted(result.output)  # 上游输出是数据,不是给下游的指令
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


def _timeout_result(agent: Agent, timeout: float | None, task_timeout: float | None) -> AgentResult:
    """超时任务的类型化结果(只带 code / 预算,不带任何正文)。"""
    exc = AgentTimeoutError("agent 超时未返回", timeout=timeout, task_timeout=task_timeout)
    return AgentResult(agent=agent.name, output="", error=error_to_dict(exc))
