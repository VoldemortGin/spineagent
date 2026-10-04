"""多轮多审批:raise 模式的重跑放大对审批人可见(prior_executions)、feed_back 每个动作恰好执行一次。

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

from spineagent.agent.approval import (
    ApprovalMiddleware,
    ApprovalPending,
    Decision,
    ManualApprovalGate,
    make_approval_request,
)
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.agent.middleware import MiddlewareAgent
from spineagent.tools.function_tool import FunctionTool


class _Pay:
    """带副作用计数的真实工具:每执行一次记一笔(参数值)。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def tool(self) -> FunctionTool:
        def pay(value: str) -> str:
            self.calls.append(value)
            return "paid"

        return FunctionTool(
            "pay",
            "",
            {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            func=pay,
        )


class _OnePerRound:
    """每轮一个 pay 调用、共 n 轮的离线 provider(无状态,按 assistant 条数定位)。"""

    def __init__(self, n: int) -> None:
        self._n = n

    def chat(self, messages, *, tools=None):
        index = sum(1 for m in messages if m.get("role") == "assistant")
        if index < self._n:
            call = LLMToolCall(
                id=f"c{index}",
                function=FunctionCall(name="pay", arguments=json.dumps({"value": f"/r{index}"})),
            )
            message = ResponseMessage(role="assistant", content=None, tool_calls=(call,))
            return ChatCompletion(choices=(Choice(index=0, message=message),))
        message = ResponseMessage(role="assistant", content="done")
        return ChatCompletion(choices=(Choice(index=0, message=message),))


def _agent(pay: _Pay, gate: ManualApprovalGate, n: int, **fc_kw) -> MiddlewareAgent:
    fc = FunctionCallingAgent("fc", _OnePerRound(n), [pay.tool()], **fc_kw)
    return MiddlewareAgent("mw", fc, [ApprovalMiddleware(gate, gated_tools=["pay"], scope="S")])


def test_feed_back_executes_each_action_exactly_once_over_three_rounds():
    pay, gate = _Pay(), ManualApprovalGate()
    first = _agent(pay, gate, 3, on_approval="feed_back").step("go")
    assert pay.calls == [] and len(first.held_approvals) == 3  # 三轮的请求一次登记完
    for held in first.held_approvals:
        gate.resolve(held["request_id"], Decision.APPROVED)
    second = _agent(pay, gate, 3, on_approval="feed_back").step("go")
    assert second.held_approvals == ()
    assert sorted(pay.calls) == ["/r0", "/r1", "/r2"]  # 每个动作恰好 1 次


def test_raise_mode_second_pending_carries_prior_executions():
    pay, gate = _Pay(), ManualApprovalGate()
    agent = _agent(pay, gate, 2)
    with pytest.raises(ApprovalPending) as ei:  # 第 1 跑:挂起 /r0
        agent.step("go")
    first_id = ei.value.context["request_id"]
    gate.resolve(first_id, Decision.APPROVED)
    with pytest.raises(ApprovalPending) as ei:  # 第 2 跑:执行 /r0(批准核销),挂起 /r1
        agent.step("go")
    gate.resolve(ei.value.context["request_id"], Decision.APPROVED)
    assert pay.calls == ["/r0"]
    with pytest.raises(ApprovalPending) as ei:  # 第 3 跑:/r0 的一次性批准已用掉,同一 id 重新挂起
        agent.step("go")
    err = ei.value
    assert err.context["request_id"] == first_id  # 与已执行过的那条 id 完全相同
    assert err.context["prior_executions"] == 1
    assert "feed_back" in str(err) and "1 次" in str(err)
    (inbox,) = gate.pending()
    assert inbox.id == first_id and inbox.prior_executions == 1
    assert any("1 次" in text for _, text in inbox.preview)  # 审批列表里一眼可见


def test_first_time_request_has_no_prior_executions():
    pay, gate = _Pay(), ManualApprovalGate()
    with pytest.raises(ApprovalPending) as ei:
        _agent(pay, gate, 1).step("go")
    assert "prior_executions" not in ei.value.context
    assert gate.pending()[0].prior_executions == 0
    assert "feed_back" in str(ei.value)  # 通用提示仍在(至少一次语义)


def test_executed_counter_table_is_bounded_and_expires():
    clock = [0.0]
    gate = ManualApprovalGate(max_decided=2, decided_ttl=100.0, now_fn=lambda: clock[0])

    def run_once(path: str):
        req = make_approval_request("tool_call", "pay", {"v": path}, bind_values=True, scope="S")
        gate.review(req)
        gate.resolve(req.id, Decision.APPROVED)
        assert gate.consume(req)
        return req

    reqs = [run_once(f"/r{i}") for i in range(5)]
    for req in reqs:
        gate.review(req)  # 重新登记为待审
    by_id = {p.id: p.prior_executions for p in gate.pending()}
    assert by_id[reqs[-1].id] == 1 and by_id[reqs[-2].id] == 1  # 最近的 2 条还记得
    assert by_id[reqs[0].id] == 0  # 超出上限的最早条目被丢弃(只是提示性计数,丢了不影响安全)
    # TTL:过期后计数清零。
    gate2 = ManualApprovalGate(decided_ttl=10.0, now_fn=lambda: clock[0])
    req = make_approval_request("tool_call", "pay", {"v": "x"}, bind_values=True, scope="S")
    gate2.review(req)
    gate2.resolve(req.id, Decision.APPROVED)
    gate2.consume(req)
    clock[0] += 11.0
    gate2.review(req)
    assert gate2.pending()[0].prior_executions == 0
