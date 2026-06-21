"""编排合约:顺序 / 并行 / 流水线跑 mock agent、保序收集、弹性容错、编排级 trace 隐私安全。"""

import threading

import pytest
from corespine.observability.trace import InProcessPrivacyTraceSink

from spineagent.agent.agent import FunctionAgent
from spineagent.orchestration.coordinator import Coordinator


def _two_mock_agents():
    return [
        FunctionAgent("a", lambda t: f"a:{t}"),
        FunctionAgent("b", lambda t: f"b:{t}"),
    ]


def _boom(_task: str) -> str:
    raise ValueError("boom")


def test_run_sequential_collects_all_in_order():
    coord = Coordinator(_two_mock_agents())
    results = coord.run_sequential("go")
    assert [r.agent for r in results] == ["a", "b"]
    assert [r.output for r in results] == ["a:go", "b:go"]


def test_run_parallel_collects_all_preserving_order():
    coord = Coordinator(_two_mock_agents())
    results = coord.run_parallel("go")
    # 并发跑,但结果仍按 agent 输入顺序返回(确定性、可断言)。
    assert [r.agent for r in results] == ["a", "b"]
    assert [r.output for r in results] == ["a:go", "b:go"]


def test_run_pipeline_threads_output_into_next_input():
    coord = Coordinator(_two_mock_agents())
    results = coord.run_pipeline("go")
    # a 先吃 "go" 产出 "a:go";b 再吃 "a:go" 产出 "b:a:go"(链式传递)。
    assert [r.agent for r in results] == ["a", "b"]
    assert [r.output for r in results] == ["a:go", "b:a:go"]


def test_run_pipeline_empty_agents_returns_empty():
    assert Coordinator([]).run_pipeline("go") == []


def test_default_is_fail_fast_exception_bubbles():
    # 非 resilient(默认):三种模式 agent 抛异常都照常冒泡原始类型(保持既有行为,不归一成 dict)。
    coord = Coordinator([FunctionAgent("a", lambda t: t), FunctionAgent("boom", _boom)])
    with pytest.raises(ValueError):
        coord.run_sequential("go")
    with pytest.raises(ValueError):
        coord.run_parallel("go")
    with pytest.raises(ValueError):
        coord.run_pipeline("go")


def test_pipeline_fail_fast_stops_and_skips_downstream():
    # 流水线非 resilient:中段失败时异常冒泡,且失败点之后的 agent 绝不执行(对称 sequential/parallel)。
    ran: list[str] = []
    coord = Coordinator(
        [
            FunctionAgent("a", lambda t: f"a:{t}"),
            FunctionAgent("boom", _boom),
            FunctionAgent("c", lambda t: ran.append("c") or "c"),
        ]
    )
    with pytest.raises(ValueError):
        coord.run_pipeline("go")
    assert ran == []  # boom 冒泡时下游 c 从未运行


def test_resilient_sequential_captures_error_and_continues():
    coord = Coordinator(
        [FunctionAgent("boom", _boom), FunctionAgent("b", lambda t: f"b:{t}")]
    )
    results = coord.run_sequential("go", resilient=True)
    # 第一个 agent 失败被捕获成结构化错误,但批次继续把第二个跑完。
    assert [r.agent for r in results] == ["boom", "b"]
    assert not results[0].ok
    assert results[0].error["type"] == "ValueError"
    assert results[0].error["code"] == "error"
    assert results[0].error["retryable"] is False
    assert results[1].ok and results[1].output == "b:go"


def test_run_parallel_is_truly_concurrent():
    # 用 Barrier 钉死「真并发」:三个 agent 必须同时到达栅栏才放行;若退化成串行,
    # 第一个会在栅栏上等不到另外两个而超时抛 BrokenBarrierError,测试随即失败。
    barrier = threading.Barrier(3, timeout=3)

    def _gated(name: str):
        def fn(task: str) -> str:
            barrier.wait()
            return f"{name}:{task}"

        return fn

    agents = [FunctionAgent(f"a{i}", _gated(f"a{i}")) for i in range(3)]
    results = Coordinator(agents).run_parallel("go")
    assert [r.output for r in results] == ["a0:go", "a1:go", "a2:go"]


def test_resilient_parallel_captures_error_preserving_order():
    coord = Coordinator(
        [FunctionAgent("a", lambda t: f"a:{t}"), FunctionAgent("boom", _boom)]
    )
    results = coord.run_parallel("go", resilient=True)
    assert [r.agent for r in results] == ["a", "boom"]
    assert results[0].ok
    assert not results[1].ok and results[1].error["message"] == "boom"


def test_resilient_parallel_captures_multiple_concurrent_failures():
    # 多个 agent 同时失败:跨线程各自归一为结构化错误,保序且互不串味。
    coord = Coordinator(
        [
            FunctionAgent("ok", lambda t: f"ok:{t}"),
            FunctionAgent("boom1", _boom),
            FunctionAgent("boom2", _boom),
        ]
    )
    results = coord.run_parallel("go", resilient=True)
    assert [r.ok for r in results] == [True, False, False]
    assert all(r.error["type"] == "ValueError" for r in results if not r.ok)


def test_resilient_all_success_matches_normal_run():
    # resilient=True 但全部成功:结果与普通跑一致,failures 计数为 0。
    sink = InProcessPrivacyTraceSink()
    results = Coordinator(_two_mock_agents(), trace=sink).run_sequential("go", resilient=True)
    assert all(r.ok for r in results)
    assert [r.output for r in results] == ["a:go", "b:go"]
    assert sink.events[0].fields["failures"] == 0


def test_ok_reflects_error_not_empty_output():
    # ok 由 error 判定,而非 output 是否为空:成功但输出为空的步仍 ok(钉死 ok 的判别语义)。
    results = Coordinator([FunctionAgent("empty", lambda t: "")]).run_sequential("go", resilient=True)
    assert results[0].ok and results[0].output == ""


def test_resilient_pipeline_stops_at_failure():
    # 流水线中段失败:下游拿不到输入,停在失败处,只返回已跑出的各段。
    coord = Coordinator(
        [
            FunctionAgent("a", lambda t: f"a:{t}"),
            FunctionAgent("boom", _boom),
            FunctionAgent("c", lambda t: f"c:{t}"),
        ]
    )
    results = coord.run_pipeline("go", resilient=True)
    assert [r.agent for r in results] == ["a", "boom"]  # c 从未运行
    assert results[0].ok and results[0].output == "a:go"
    assert not results[1].ok


def test_coordinator_emits_only_privacy_safe_summary_trace():
    sink = InProcessPrivacyTraceSink()
    coord = Coordinator(_two_mock_agents(), trace=sink)
    coord.run_sequential("go")
    coord.run_parallel("go")
    coord.run_pipeline("go")
    assert sink.codes() == ["coordinate", "coordinate", "coordinate"]
    assert [e.fields["mode"] for e in sink.events] == ["sequential", "parallel", "pipeline"]
    assert all(e.fields["agent_count"] == 2 for e in sink.events)
    assert all(e.fields["failures"] == 0 for e in sink.events)
    # 编排级 trace 只含模式 / 计数 / 失败数 / 耗时,没有任务或输出正文。
    for event in sink.events:
        assert set(event.fields) == {"mode", "agent_count", "failures", "took_ms"}


def test_resilient_trace_counts_failures():
    sink = InProcessPrivacyTraceSink()
    coord = Coordinator([FunctionAgent("boom", _boom)], trace=sink)
    coord.run_sequential("go", resilient=True)
    # 失败计数进编排级 trace(隐私安全:只记数量,不记错误正文)。
    assert sink.events[0].fields["failures"] == 1
