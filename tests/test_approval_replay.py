"""修复 3:等待审批期间的重跑不得重放其它工具的副作用(步内记账 / 先审后行 / 不中断模式 / 并行分支)。

一律走真实执行路径:真实 FunctionCallingAgent + 离线脚本化 provider + 带副作用计数的真实工具函数。
"""

import json

import pytest
from corespine.llm.provider import (
    ChatCompletion,
    Choice,
    FunctionCall,
    ResponseMessage,
)
from corespine.llm.provider import ToolCall as LLMToolCall

from spineagent.agent.agent import FunctionAgent
from spineagent.agent.approval import (
    ApprovalMiddleware,
    ApprovalPending,
    Decision,
    ManualApprovalGate,
    approval_scope,
)
from spineagent.agent.builtin.deep_research import DeepResearchAgent
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.agent.middleware import MiddlewareAgent
from spineagent.conformance import ScriptedToolCallProvider
from spineagent.orchestration.coordinator import Coordinator
from spineagent.tools.function_tool import FunctionTool


class _Log:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def tool(self, name: str) -> FunctionTool:
        def run(value: str) -> str:
            self.calls.append((name, value))
            return f"{name}:{value}"

        return FunctionTool(
            name,
            "",
            {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            func=run,
        )

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


class _BatchProvider:
    """每轮回放一批(可多个)tool_calls 的离线 provider(无状态,按 assistant 条数定位)。"""

    def __init__(self, batches, final="finished") -> None:
        self._batches = list(batches)
        self._final = final

    def chat(self, messages, *, tools=None):
        index = sum(1 for m in messages if m.get("role") == "assistant")
        if index < len(self._batches):
            calls = tuple(
                LLMToolCall(
                    id=f"call_{index}_{j}",
                    function=FunctionCall(name=name, arguments=json.dumps({"value": value})),
                )
                for j, (name, value) in enumerate(self._batches[index])
            )
            message = ResponseMessage(role="assistant", content=None, tool_calls=calls)
            return ChatCompletion(choices=(Choice(index=0, message=message),))
        message = ResponseMessage(role="assistant", content=self._final)
        return ChatCompletion(choices=(Choice(index=0, message=message),))


def _agent(log, gate, script, tools=("send_email", "delete_file"), gated=("delete_file",), **fc_kw):
    fc = FunctionCallingAgent("fc", script, [log.tool(t) for t in tools], **fc_kw)
    return MiddlewareAgent("mw", fc, [ApprovalMiddleware(gate, gated_tools=list(gated))])


def _pending(agent):
    with pytest.raises(ApprovalPending) as ei:
        agent.step("t")
    return ei.value


def test_review_a4_rerun_while_pending_does_not_replay_other_side_effects():
    # 审查 A4:send_email 在前、受审批工具在后;pending 时重跑 3 次、resolve 后再跑 1 次,
    # send_email 共执行了 4 次。现在:同一作用域里的重跑复用已执行调用的记录结果。
    log, gate = _Log(), ManualApprovalGate()
    script = ScriptedToolCallProvider(
        [("send_email", {"value": "boss"}), ("delete_file", {"value": "/a"})], final="finished"
    )
    agent = _agent(log, gate, script)
    with approval_scope("session-1"):
        for _ in range(3):
            request_id = _pending(agent).context["request_id"]
        gate.resolve(request_id, Decision.APPROVED)
        assert agent.step("t").output == "finished"
    assert log.count("send_email") == 1
    assert log.count("delete_file") == 1


def test_step_with_two_gated_calls_can_be_resumed_with_single_use_approvals():
    # 一次性消费下:前面已获批并执行过的调用在重跑时由记账复用,不需要也不会再次执行 / 再次审批。
    log, gate = _Log(), ManualApprovalGate()
    script = ScriptedToolCallProvider(
        [
            ("delete_file", {"value": "/a"}),
            ("send_email", {"value": "x"}),
            ("delete_file", {"value": "/b"}),
        ],
        final="finished",
    )
    agent = _agent(log, gate, script)
    with approval_scope("session-1"):
        gate.resolve(_pending(agent).context["request_id"], Decision.APPROVED)
        gate.resolve(_pending(agent).context["request_id"], Decision.APPROVED)
        assert agent.step("t").output == "finished"
    assert log.calls == [("delete_file", "/a"), ("send_email", "x"), ("delete_file", "/b")]


def test_approve_before_execute_runs_nothing_in_a_batch_until_all_approved():
    # 先审后行(缺省):同一轮 tool_calls 里有任何一个需要审批且未获批,该轮一个工具都不执行。
    log, gate = _Log(), ManualApprovalGate()
    script = _BatchProvider([[("send_email", "boss"), ("delete_file", "/a")]])
    agent = _agent(log, gate, script)
    with approval_scope("session-1"):
        request_id = _pending(agent).context["request_id"]
        assert log.calls == []
        gate.resolve(request_id, Decision.APPROVED)
        agent.step("t")
    assert log.calls == [("send_email", "boss"), ("delete_file", "/a")]


def test_approve_before_execute_can_be_turned_off():
    log, gate = _Log(), ManualApprovalGate()
    script = _BatchProvider([[("send_email", "boss"), ("delete_file", "/a")]])
    agent = _agent(log, gate, script, approve_before_execute=False)
    _pending(agent)
    assert log.calls == [("send_email", "boss")]  # 逐个执行:闸只拦受审批的那一个


def test_feed_back_mode_reports_approval_to_the_model_instead_of_raising():
    log, gate = _Log(), ManualApprovalGate()
    seen: list[str] = []

    class Recording(_BatchProvider):
        def chat(self, messages, *, tools=None):
            seen.extend(m["content"] for m in messages if m.get("role") == "tool")
            return super().chat(messages, tools=tools)

    agent = _agent(
        log,
        gate,
        Recording([[("send_email", "boss"), ("delete_file", "/a")]]),
        on_approval="feed_back",
    )
    assert agent.step("t").output == "finished"
    assert log.calls == []  # 先审后行:整轮未执行
    assert any("approval.pending" in text for text in seen)
    assert len(gate.pending()) == 1  # 审批人照样能看到待审请求


def test_parallel_branches_do_not_replay_on_resume():
    # DeepResearch 的并行分支:每个分支先 send_email 再调受审批工具;resume 后已执行的 send 不重放。
    log, gate = _Log(), ManualApprovalGate()
    script = ScriptedToolCallProvider(
        [("send_email", {"value": "boss"}), ("delete_file", {"value": "/a"})]
    )
    research = DeepResearchAgent(
        provider=script,
        tools=[log.tool("send_email"), log.tool("delete_file")],
        planner=lambda task: ["q1", "q2"],
    )
    agent = MiddlewareAgent("mw", research, [ApprovalMiddleware(gate, gated_tools=["delete_file"])])
    with approval_scope("session-1"):
        for _ in range(2):
            request_id = _pending(agent).context["request_id"]
        gate.resolve(request_id, Decision.APPROVED, uses=2)  # 两个分支各执行一次
        agent.step("t")
    assert log.count("send_email") == 2  # 每个分支一次,重跑不重放
    assert log.count("delete_file") == 2


def test_run_parallel_inside_wrapped_step_does_not_replay_completed_branch():
    log, gate = _Log(), ManualApprovalGate()
    risky = FunctionCallingAgent(
        "risky",
        ScriptedToolCallProvider([("delete_file", {"value": "/a"})]),
        [log.tool("delete_file")],
    )
    benign = FunctionCallingAgent(
        "benign",
        ScriptedToolCallProvider([("send_email", {"value": "b"})]),
        [log.tool("send_email")],
    )
    fan = FunctionAgent("fan", lambda t: Coordinator([benign, risky]).run_parallel(t)[0].output)
    agent = MiddlewareAgent(
        "mw", fan, [ApprovalMiddleware(gate, gated_tools=["delete_file"], scope="session-1")]
    )
    request_id = _pending(agent).context["request_id"]
    _pending(agent)
    gate.resolve(request_id, Decision.APPROVED)
    agent.step("t")
    assert log.count("send_email") == 1
    assert log.count("delete_file") == 1


def test_successful_step_clears_the_ledger():
    # 记账只对「因审批挂起而重跑」生效:成功完成后再跑一遍,工具照常执行。
    log, gate = _Log(), ManualApprovalGate()
    script = ScriptedToolCallProvider([("send_email", {"value": "boss"})])
    agent = _agent(log, gate, script)
    with approval_scope("session-1"):
        agent.step("t")
        agent.step("t")
    assert log.count("send_email") == 2
