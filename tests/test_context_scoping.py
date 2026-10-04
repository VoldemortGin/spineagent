"""修复 4:审批作用域 / trace 不跨请求泄漏;上下文跨线程的显式传递;run_parallel 带超时路径的上下文复制。"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from corespine.observability.trace import InProcessPrivacyTraceSink

from spineagent.agent.agent import FunctionAgent
from spineagent.agent.approval import (
    ApprovalMiddleware,
    ApprovalRejected,
    AutoApprovalGate,
    current_approval_scope,
)
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.agent.middleware import MiddlewareAgent, StepContext
from spineagent.conformance import ScriptedToolCallProvider
from spineagent.orchestration.coordinator import Coordinator
from spineagent.tools.function_tool import FunctionTool


class _Deleter:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def __call__(self, path: str) -> str:
        self.paths.append(path)
        return "deleted"


def _tool(deleter: _Deleter) -> FunctionTool:
    return FunctionTool(
        "delete_file",
        "",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        func=deleter,
    )


def _fc(deleter: _Deleter, name: str = "fc") -> FunctionCallingAgent:
    script = ScriptedToolCallProvider([("delete_file", {"path": "/z"})], final="finished")
    return FunctionCallingAgent(name, script, [_tool(deleter)])


def _deny_all() -> ApprovalMiddleware:
    return ApprovalMiddleware(AutoApprovalGate(deny=["*"]), gated_tools=["delete_file"])


class _BadCleanup:
    def __init__(self) -> None:
        self.ran: list[str] = []

    def before_step(self, ctx: StepContext) -> None:
        def boom() -> None:
            raise RuntimeError("cleanup failed")

        ctx.cleanups.append(boom)

    def after_step(self, ctx, result):
        return result


def test_review_a3_failing_cleanup_does_not_leak_into_next_request():
    # 审查 A3:自定义中间件登记的 cleanup 抛异常时,审批作用域的弹出被跳过——同一线程里之后一个
    # 完全没配审批的 agent 被 ApprovalRejected 拦下,且请求 1 的 trace sink 收到了请求 2 的事件。
    sink_req1 = InProcessPrivacyTraceSink()
    idle = FunctionCallingAgent("idle", ScriptedToolCallProvider([]), [_tool(_Deleter())])
    req1 = MiddlewareAgent("req1", idle, [_deny_all(), _BadCleanup()])
    with pytest.raises(RuntimeError):
        req1.step("hi", trace=sink_req1)
    before = len(sink_req1.events)

    deleter = _Deleter()
    assert _fc(deleter, name="req2").step("unrelated").output == "finished"
    assert deleter.paths == ["/z"]
    assert len(sink_req1.events) == before
    assert current_approval_scope() is None


def test_every_cleanup_runs_even_if_an_earlier_one_raises():
    ran: list[str] = []

    class Registers:
        def before_step(self, ctx: StepContext) -> None:
            ctx.cleanups.append(lambda: ran.append("first-registered"))

        def after_step(self, ctx, result):
            return result

    agent = MiddlewareAgent("m", FunctionAgent("f", lambda t: "ok"), [Registers(), _BadCleanup()])
    with pytest.raises(RuntimeError):
        agent.step("t")
    assert ran == ["first-registered"]


def test_step_error_wins_over_cleanup_error():
    def explode(task: str) -> str:
        raise ValueError("inner failed")

    agent = MiddlewareAgent("m", FunctionAgent("f", explode), [_BadCleanup()])
    with pytest.raises(ValueError) as ei:
        agent.step("t")
    assert any("cleanup" in note for note in getattr(ei.value, "__notes__", []))


def test_bind_context_carries_approval_scope_into_user_threads():
    # 审查 A2:工具函数内部用 ThreadPoolExecutor 跑子 agent,动态作用域不跟过去。辅助函数把当前上下文带进去。
    from spineagent.orchestration.coordinator import bind_context

    deleter = _Deleter()
    inner = _fc(deleter, name="inner")

    def fanout(path: str) -> str:
        with ThreadPoolExecutor(1) as pool:
            return pool.submit(bind_context(inner.step), "go").result().output

    fan = FunctionTool(
        "fanout", "", {"type": "object", "properties": {"path": {"type": "string"}}}, fanout
    )
    outer = FunctionCallingAgent(
        "outer", ScriptedToolCallProvider([("fanout", {"path": "p"})]), [fan]
    )
    agent = MiddlewareAgent(
        "mw", FunctionAgent("opaque", lambda t: outer.step(t).output), [_deny_all()]
    )
    with pytest.raises(ApprovalRejected):
        agent.step("t")
    assert deleter.paths == []


def test_run_parallel_with_timeout_carries_approval_scope():
    # 带超时路径(_run_parallel_bounded)同样把调用方上下文复制进工作线程。
    deleter = _Deleter()
    fc = _fc(deleter)
    fan = FunctionAgent("fan", lambda t: Coordinator([fc]).run_parallel(t, timeout=30.0)[0].output)
    with pytest.raises(ApprovalRejected):
        MiddlewareAgent("mw", fan, [_deny_all()]).step("t")
    assert deleter.paths == []


def test_run_parallel_task_timeout_carries_approval_scope():
    deleter = _Deleter()
    fc = _fc(deleter)
    fan = FunctionAgent(
        "fan", lambda t: Coordinator([fc]).run_parallel(t, task_timeout=30.0)[0].output
    )
    with pytest.raises(ApprovalRejected):
        MiddlewareAgent("mw", fan, [_deny_all()]).step("t")
    assert deleter.paths == []


def test_concurrent_requests_do_not_share_scope():
    seen: dict[str, str | None] = {}
    barrier = threading.Barrier(2)

    def probe(label: str):
        def fn(task: str) -> str:
            barrier.wait(timeout=5)
            seen[label] = current_approval_scope()
            return "ok"

        return fn

    def run(label: str, scope: str) -> None:
        mw = ApprovalMiddleware(AutoApprovalGate(), gated_tools=["delete_file"], scope=scope)
        MiddlewareAgent("m", FunctionAgent(label, probe(label)), [mw]).step("t")

    threads = [threading.Thread(target=run, args=(f"r{i}", f"scope-{i}")) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"r0": "scope-0", "r1": "scope-1"}


def test_context_set_inside_a_step_never_leaks_even_without_cleanup():
    # 结构性保证:middleware 在 before_step 里设了 contextvar 却没登记收尾,也不会泄漏给调用方。
    import contextvars

    probe: contextvars.ContextVar[str | None] = contextvars.ContextVar("probe", default=None)

    class Sloppy:
        def before_step(self, ctx: StepContext) -> None:
            probe.set("leaked")

        def after_step(self, ctx, result):
            return result

    MiddlewareAgent("m", FunctionAgent("f", lambda t: "ok"), [Sloppy()]).step("t")
    assert probe.get() is None
