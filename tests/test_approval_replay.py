"""审批挂起与重跑:先审后行 / 不中断模式 / 至少一次的重跑语义 / 不存在跨 run 的结果复用。

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
    approval_scope,
)
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.agent.middleware import MiddlewareAgent
from spineagent.conformance import ScriptedToolCallProvider
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


def _scoped(log, gate, script, **fc_kw):
    fc = FunctionCallingAgent(
        "fc", script, [log.tool(t) for t in ("send_email", "delete_file")], **fc_kw
    )
    return MiddlewareAgent(
        "mw", fc, [ApprovalMiddleware(gate, gated_tools=["delete_file"], scope="session-1")]
    )


def _pending(agent):
    with pytest.raises(ApprovalPending) as ei:
        agent.step("t")
    return ei.value


class _Profile:
    """无参工具:返回某个租户的私有数据(带副作用计数)。"""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.hits = 0

    def __call__(self) -> str:
        self.hits += 1
        return f"secret-of-{self.owner}"


class _SeenToolMessages(_BatchProvider):
    """记录喂回给模型的 tool 消息。"""

    def __init__(self, batches, final="finished") -> None:
        super().__init__(batches, final)
        self.seen: list[str] = []

    def chat(self, messages, *, tools=None):
        self.seen = [m["content"] for m in messages if m.get("role") == "tool"]
        index = sum(1 for m in messages if m.get("role") == "assistant")
        if index < len(self._batches):
            calls = tuple(
                LLMToolCall(
                    id=f"call_{index}_{j}",
                    function=FunctionCall(name=name, arguments=json.dumps(args)),
                )
                for j, (name, args) in enumerate(self._batches[index])
            )
            message = ResponseMessage(role="assistant", content=None, tool_calls=calls)
            return ChatCompletion(choices=(Choice(index=0, message=message),))
        message = ResponseMessage(role="assistant", content=self._final)
        return ChatCompletion(choices=(Choice(index=0, message=message),))


def test_review_r2_new_turn_in_same_session_really_executes_side_effects():
    # 复审 L:按文档推荐用 scope="session-42";第 1 轮 send_email 执行后挂起;第 2 轮是【新任务】,同参数
    # 的 send_email 没有执行,模型却被告知 ok。现在没有任何跨 run 的结果复用:用户新要求的副作用必须真的发生。
    log, gate = _Log(), ManualApprovalGate()
    turn1 = _SeenToolMessages(
        [[("send_email", {"value": "boss"})], [("delete_file", {"value": "/x"})]]
    )
    agent1 = _agent(log, gate, turn1)
    with approval_scope("session-42"):
        _pending(agent1)
    assert log.count("send_email") == 1
    turn2 = _SeenToolMessages([[("send_email", {"value": "boss"})]])
    with approval_scope("session-42"):
        assert _agent(log, gate, turn2).step("send it again").output == "finished"
    assert log.count("send_email") == 2
    assert turn2.seen == ["send_email:boss"]


def test_review_r2_shared_scope_string_never_leaks_results_or_approvals_across_tenants():
    # 复审 C:两个租户共用一个门、作用域字符串都叫 chat-1 —— Bob 的模型拿到了 Alice 工具的返回值(他自己的
    # 工具一次都没执行);批准 Alice 的请求后 Bob 的 run 执行了 delete_file。
    gate = ManualApprovalGate()
    deleted: list[str] = []

    def build(owner: str):
        profile = _Profile(owner)
        script = _SeenToolMessages(
            [[("read_profile", {})], [("delete_file", {"value": f"/home/{owner}/x"})]]
        )
        tools = [
            FunctionTool("read_profile", "", {"type": "object", "properties": {}}, func=profile),
            FunctionTool(
                "delete_file",
                "",
                {"type": "object", "properties": {"value": {"type": "string"}}},
                func=lambda value: deleted.append(value) or "deleted",
            ),
        ]
        fc = FunctionCallingAgent("fc", script, tools)
        mw = ApprovalMiddleware(gate, gated_tools=["delete_file"], scope="chat-1")
        return MiddlewareAgent("mw", fc, [mw]), profile, script

    alice, alice_profile, _ = build("alice")
    alice_id = _pending(alice).context["request_id"]
    bob, bob_profile, bob_script = build("bob")
    bob_id = _pending(bob).context["request_id"]
    assert bob_profile.hits == 1  # Bob 自己的工具真的执行了
    assert bob_script.seen == ["secret-of-bob"]  # Bob 的模型只看到 Bob 的数据
    assert alice_profile.hits == 1
    assert bob_id != alice_id
    gate.resolve(alice_id, Decision.APPROVED)
    _pending(bob)  # Alice 的批准对 Bob 的调用无效
    assert deleted == []
    alice.step("t")
    assert deleted == ["/home/alice/x"]


def test_rerun_after_pending_is_at_least_once():
    # 语义钉子(ADR 0002 决策 5a):挂起后重跑整个 run,此前已执行过的非受审批工具会再次执行——库不记录、
    # 不复用任何跨 run 的工具结果。要「不重放」就让工具幂等,或用 on_approval="feed_back"。
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
    assert log.count("send_email") == 4
    assert log.count("delete_file") == 1


def test_feed_back_mode_has_no_rerun_and_no_replay():
    # 不中断模式:挂起作为 tool 结果喂回模型,run 正常结束;批准后由【下一个任务】去执行受审批调用。
    log, gate = _Log(), ManualApprovalGate()
    first = _SeenToolMessages(
        [[("send_email", {"value": "boss"})], [("delete_file", {"value": "/a"})]]
    )
    with approval_scope("session-1"):
        assert _agent(log, gate, first, on_approval="feed_back").step("t").output == "finished"
    assert log.calls == [("send_email", "boss")]
    [request] = gate.pending()
    gate.resolve(request.id, Decision.APPROVED)
    follow_up = _SeenToolMessages([[("delete_file", {"value": "/a"})]])
    with approval_scope("session-1"):
        _agent(log, gate, follow_up, on_approval="feed_back").step("now delete it")
    assert log.calls == [("send_email", "boss"), ("delete_file", "/a")]


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
    agent = _scoped(log, gate, script, approve_before_execute=False)
    _pending(agent)
    assert log.calls == [("send_email", "boss")]  # 逐个执行:闸只拦受审批的那一个


def test_feed_back_mode_reports_approval_to_the_model_instead_of_raising():
    log, gate = _Log(), ManualApprovalGate()
    seen: list[str] = []

    class Recording(_BatchProvider):
        def chat(self, messages, *, tools=None):
            seen.extend(m["content"] for m in messages if m.get("role") == "tool")
            return super().chat(messages, tools=tools)

    agent = _scoped(
        log,
        gate,
        Recording([[("send_email", "boss"), ("delete_file", "/a")]]),
        on_approval="feed_back",
    )
    assert agent.step("t").output == "finished"
    assert log.calls == []  # 先审后行:整轮未执行
    assert any("approval.pending" in text for text in seen)
    assert len(gate.pending()) == 1  # 审批人照样能看到待审请求


def test_per_call_path_feeds_back():
    # 关掉先审后行:喂回模式在逐个执行路径(执行闸 check)上也不抛。
    log2, gate2 = _Log(), ManualApprovalGate()
    fed = _scoped(
        log2,
        gate2,
        _BatchProvider([[("send_email", "boss"), ("delete_file", "/a")]]),
        approve_before_execute=False,
        on_approval="feed_back",
    )
    assert fed.step("t").output == "finished"
    assert log2.calls == [("send_email", "boss")]
