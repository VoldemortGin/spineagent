"""审批请求表的配额:少数会话 / 一个作用域不能把全表占满而拒绝所有人;已决议的记录不占待审配额。

攻击形态来自复审复现:16 个会话各发一轮 64 个调用;单作用域反复发 64 个、被审批人拒绝后仍占全表名额。
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
    ApprovalConflict,
    ApprovalGateError,
    ApprovalMiddleware,
    ApprovalPending,
    ApprovalRejected,
    ApprovalRequest,
    ConsumableApprovalGate,
    Decision,
    ManualApprovalGate,
    make_approval_request,
)
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.agent.middleware import MiddlewareAgent
from spineagent.tools.function_tool import FunctionTool


class _Delete:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def tool(self) -> FunctionTool:
        def delete_file(value: str) -> str:
            self.calls.append(value)
            return "deleted"

        return FunctionTool(
            "delete_file",
            "",
            {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            func=delete_file,
        )


class _Batch:
    """第一轮一次发出 n 个 delete_file 调用的离线 provider。"""

    def __init__(self, tag: str, n: int) -> None:
        self._tag, self._n = tag, n

    def chat(self, messages, *, tools=None):
        if not any(m.get("role") == "assistant" for m in messages):
            calls = tuple(
                LLMToolCall(
                    id=f"c{i}",
                    function=FunctionCall(
                        name="delete_file", arguments=json.dumps({"value": f"{self._tag}-{i}"})
                    ),
                )
                for i in range(self._n)
            )
            message = ResponseMessage(role="assistant", content=None, tool_calls=calls)
            return ChatCompletion(choices=(Choice(index=0, message=message),))
        message = ResponseMessage(role="assistant", content="done")
        return ChatCompletion(choices=(Choice(index=0, message=message),))


def _session(delete: _Delete, gate, scope: str, tag: str, n: int) -> MiddlewareAgent:
    fc = FunctionCallingAgent("fc", _Batch(tag, n), [delete.tool()])
    return MiddlewareAgent(
        "mw", fc, [ApprovalMiddleware(gate, gated_tools=["delete_file"], scope=scope)]
    )


def _req(scope: str, i: int) -> ApprovalRequest:
    return make_approval_request("tool_call", "rm", {"p": str(i)}, bind_values=True, scope=scope)


def test_sixteen_sessions_filling_their_own_quota_do_not_block_another_user():
    delete, gate = _Delete(), ManualApprovalGate()
    for s in range(16):  # 复审:16 会话 × 64 调用 = 1024 恰好占满旧的全表上限
        with pytest.raises(ApprovalPending):
            _session(delete, gate, f"attacker-{s}", f"a{s}", 64).step("go")
    with pytest.raises(ApprovalPending):  # 其它用户的合法请求照常挂起,而不是 ApprovalGateError
        _session(delete, gate, "victim", "v", 1).step("go")
    assert delete.calls == []


def test_one_session_is_limited_only_in_its_own_scope():
    delete, gate = _Delete(), ManualApprovalGate()
    with pytest.raises(ApprovalPending):
        _session(delete, gate, "heavy", "h", 64).step("go")
    with pytest.raises(ApprovalGateError):  # 该作用域已满:再来一条被拒(fail-closed)
        _session(delete, gate, "heavy", "h2", 1).step("go")
    with pytest.raises(ApprovalPending):  # 别人不受影响
        _session(delete, gate, "other", "o", 64).step("go")
    assert delete.calls == []


def test_rejected_requests_do_not_occupy_the_pending_quota():
    delete, gate = _Delete(), ManualApprovalGate()
    for batch in range(16):  # 复审:单作用域被拒绝 16 批后旧实现全表满
        with pytest.raises(ApprovalPending):
            _session(delete, gate, "spammer", f"s{batch}", 64).step("go")
        for request in gate.pending():
            gate.resolve(request.id, Decision.REJECTED)
    assert gate.pending() == []
    with pytest.raises(ApprovalPending):  # 同一作用域也能继续送审(拒绝的不占待审名额)
        _session(delete, gate, "spammer", "next", 64).step("go")
    with pytest.raises(ApprovalPending):
        _session(delete, gate, "victim", "v", 1).step("go")
    assert delete.calls == []


def test_a_rejection_still_rejects_the_same_request():
    gate = ManualApprovalGate()
    request = _req("S", 0)
    gate.review(request)
    gate.resolve(request.id, Decision.REJECTED)
    assert gate.review(request) is Decision.REJECTED  # 已决议的拒绝仍被记住(幂等 / 去重)
    gate.resolve(request.id, Decision.REJECTED)  # 同决议重复 resolve 幂等
    with pytest.raises(ApprovalConflict):
        gate.resolve(request.id, Decision.APPROVED)


def test_rejection_memory_is_bounded_and_expires_to_pending_never_approved():
    clock = [0.0]
    gate = ManualApprovalGate(max_decided=2, decided_ttl=100.0, now_fn=lambda: clock[0])
    reqs = [_req("S", i) for i in range(4)]
    for r in reqs:
        gate.review(r)
        gate.resolve(r.id, Decision.REJECTED)
    assert gate.review(reqs[-1]) is Decision.REJECTED  # 最近的还记得
    assert (
        gate.review(reqs[0]) is Decision.PENDING
    )  # 超出上限的最早条目被丢弃:重新待审,绝不会变成批准
    clock[0] += 101.0
    assert gate.review(reqs[-2]) is Decision.PENDING  # TTL 过后同理


def test_pending_quota_counts_unconsumed_approvals_and_only_hurts_its_own_scope():
    gate = ManualApprovalGate(max_pending_per_scope=3, max_requests=100)
    mine = [_req("mine", i) for i in range(3)]
    for r in mine:
        gate.review(r)
        gate.resolve(r.id, Decision.APPROVED)  # 已批准但尚未核销:仍占本作用域名额
    with pytest.raises(ApprovalGateError):
        gate.review(_req("mine", 99))
    assert gate.review(_req("yours", 0)) is Decision.PENDING
    assert gate.consume(mine[0])  # 核销完即释放名额
    assert gate.review(_req("mine", 99)) is Decision.PENDING


def test_full_table_rejects_new_requests_without_evicting_anyone():
    gate = ManualApprovalGate(max_requests=2, max_pending_per_scope=2)
    a, b = _req("A", 0), _req("B", 0)
    gate.review(a)
    gate.review(b)
    gate.resolve(b.id, Decision.APPROVED)
    with pytest.raises(ApprovalGateError):
        gate.review(_req("C", 0))
    assert {p.id for p in gate.pending()} == {a.id}  # 没有淘汰别人的待审
    assert gate.consume(b)  # 也没有淘汰别人的已批准


def test_defaults_decouple_global_cap_from_per_scope_cap():
    gate = ManualApprovalGate()
    assert isinstance(gate, ConsumableApprovalGate)
    # 缺省全表上限不再等于 16 个满配额会话(旧值 1024):24 个满配额会话都容得下。
    for s in range(24):
        for i in range(64):
            gate.review(_req(f"s{s}", i))
    assert len(gate.pending()) == 24 * 64


def test_rejected_run_still_raises_rejected_on_rerun():
    delete, gate = _Delete(), ManualApprovalGate()
    agent = _session(delete, gate, "S", "x", 1)
    with pytest.raises(ApprovalPending):
        agent.step("go")
    for request in gate.pending():
        gate.resolve(request.id, Decision.REJECTED)
    with pytest.raises(ApprovalRejected):
        agent.step("go")
    assert delete.calls == []
