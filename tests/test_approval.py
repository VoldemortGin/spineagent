"""approval 缝的单元测试:审批门三态 + 一次性 resume token + 挂起/恢复 + 零行为变化回归。

conformance 已把「review 返回 Decision + gate 有名 + review 幂等 + 请求摘要不含正文」参数化钉死
(见 test_conformance.py);这里补两个默认门的专属策略语义、resume token 一次性(重放必败)、审批
middleware 的放行/断路/挂起三路、以及默认配置下的零行为变化对照。
"""

import pytest
from corespine.observability.trace import FORBIDDEN_KEYS, InProcessPrivacyTraceSink

from spineagent.agent.agent import FunctionAgent
from spineagent.agent.approval import (
    ApprovalConflict,
    ApprovalMiddleware,
    ApprovalPending,
    ApprovalRejected,
    AutoApprovalGate,
    Decision,
    InMemoryResumeTokenStore,
    InvalidResumeToken,
    ManualApprovalGate,
    approval_gates,
    make_approval_gate,
    make_approval_request,
)
from spineagent.agent.middleware import (
    DynamicToolMiddleware,
    MiddlewareAgent,
    StepContext,
    middlewares,
)

_SENTINEL = "绝密参数值SENTINEL"


# ---- make_approval_request:摘要只取 schema 指纹与计数,绝不含值 -----------------------------


def test_request_digest_excludes_argument_values():
    req = make_approval_request("tool_call", "delete_file", {"path": _SENTINEL})
    # 定位摘要各字段都不含参数值。
    assert _SENTINEL not in (req.code + req.id + req.tool + req.arg_fingerprint)
    assert req.arg_count == 1
    assert req.tool == "delete_file"


def test_request_id_is_value_insensitive_but_schema_sensitive():
    # 值不同、schema 相同 -> 同一 request id(schema 指纹只覆盖键名 + 值类型)。
    a = make_approval_request("tool_call", "rm", {"path": "/a"})
    b = make_approval_request("tool_call", "rm", {"path": "/b"})
    assert a.id == b.id and a.arg_fingerprint == b.arg_fingerprint
    # 键名不同 -> 指纹与 id 不同。
    c = make_approval_request("tool_call", "rm", {"target": "/a"})
    assert c.id != a.id
    # nonce 强制区分 schema 相同的两次调用(按次审批)。
    d = make_approval_request("tool_call", "rm", {"path": "/a"}, nonce="call-2")
    assert d.id != a.id


# ---- AutoApprovalGate:策略表(deny > allow > default)---------------------------------------


def test_auto_gate_default_permissive():
    gate = AutoApprovalGate()
    assert gate.review(make_approval_request("tool_call", "anything")) is Decision.APPROVED


def test_auto_gate_deny_beats_allow_and_glob():
    gate = AutoApprovalGate(allow=["db_*"], deny=["db_drop*"], default=Decision.REJECTED)
    assert gate.review(make_approval_request("tool_call", "db_read")) is Decision.APPROVED
    # deny 优先于 allow。
    assert gate.review(make_approval_request("tool_call", "db_drop_table")) is Decision.REJECTED
    # 都不撞 -> default。
    assert gate.review(make_approval_request("tool_call", "http_get")) is Decision.REJECTED


def test_auto_gate_rejects_pending_default():
    with pytest.raises(ValueError):
        AutoApprovalGate(default=Decision.PENDING)


# ---- ManualApprovalGate:登记 -> resolve -> 决议;pending() 列举;冲突 resolve ----------------


def test_manual_gate_pending_until_resolved():
    gate = ManualApprovalGate()
    req = make_approval_request("tool_call", "rm", {"path": "/x"})
    assert gate.review(req) is Decision.PENDING
    assert [r.id for r in gate.pending()] == [req.id]  # 已登记待审
    gate.resolve(req.id, Decision.APPROVED)
    assert gate.review(req) is Decision.APPROVED  # 幂等落决议
    assert gate.pending() == []  # 已决议不再待审


def test_manual_gate_idempotent_and_conflict():
    gate = ManualApprovalGate()
    req = make_approval_request("tool_call", "rm")
    gate.resolve(req.id, Decision.APPROVED)
    gate.resolve(req.id, Decision.APPROVED)  # 相同决议重复 resolve 幂等
    assert gate.review(req) is Decision.APPROVED
    with pytest.raises(ApprovalConflict):
        gate.resolve(req.id, Decision.REJECTED)  # 冲突决议:先落者胜


def test_manual_resolve_rejects_pending_target():
    gate = ManualApprovalGate()
    with pytest.raises(ValueError):
        gate.resolve("some-id", Decision.PENDING)


# ---- 一次性 resume token:签发一次、重放必败 ------------------------------------------------


def test_resume_token_is_single_use():
    gate = ManualApprovalGate()
    req = make_approval_request("tool_call", "rm")
    token = gate.resolve(req.id, Decision.APPROVED)
    ticket = gate.redeem(token)  # 首次 redeem 成功
    assert ticket.request_id == req.id and ticket.decision is Decision.APPROVED
    with pytest.raises(InvalidResumeToken):
        gate.redeem(token)  # 重放必败


def test_resume_token_unknown_rejected():
    store = InMemoryResumeTokenStore()
    with pytest.raises(InvalidResumeToken):
        store.redeem("never-issued")


def test_resume_token_only_hash_stored():
    # 落表只存 sha256 哈希:明文 token 绝不出现在存储内部结构里(隐私)。
    store = InMemoryResumeTokenStore()
    raw = store.issue("req-1", Decision.APPROVED)
    assert raw not in store._records  # 明文不作键
    assert repr(store) == "InMemoryResumeTokenStore(records=1)"  # repr 只暴露计数


# ---- 工厂 + 注册表 --------------------------------------------------------------------------


def test_make_approval_gate_factory():
    assert isinstance(make_approval_gate("auto"), AutoApprovalGate)
    assert isinstance(make_approval_gate("manual"), ManualApprovalGate)
    assert set(approval_gates.names()) >= {"auto", "manual"}
    with pytest.raises(ValueError):
        make_approval_gate("nope")


def test_approval_middleware_registered():
    mw = middlewares.make("approval", gate=AutoApprovalGate())
    assert isinstance(mw, ApprovalMiddleware)


# ---- ApprovalMiddleware:放行 / 断路 / 挂起 三路 ---------------------------------------------


def _seeded_agent(gate, *, gated_tools, tool_for_step):
    """把 DynamicToolMiddleware(播种 ctx.tools)套在审批 middleware 外层,内层是会「执行」的 agent。"""
    inner = FunctionAgent("inner", lambda t: "did-risky-thing")
    seed = DynamicToolMiddleware(default=tuple(tool_for_step))
    return MiddlewareAgent("guarded", inner, [seed, ApprovalMiddleware(gate, gated_tools=gated_tools)])


def test_default_config_is_zero_behavior_change():
    # 空 gated_tools:同一输入,加不加审批 middleware 输出 + trace code 序列全等(回归对照)。
    def make_inner() -> FunctionAgent:
        return FunctionAgent("inner", lambda t: f"out:{t}")

    plain = MiddlewareAgent("g", make_inner(), [DynamicToolMiddleware(default=("delete_file",))])
    guarded = MiddlewareAgent(
        "g",
        make_inner(),
        [DynamicToolMiddleware(default=("delete_file",)), ApprovalMiddleware(AutoApprovalGate())],
    )
    s1, s2 = InProcessPrivacyTraceSink(), InProcessPrivacyTraceSink()
    r_plain = plain.step("go", trace=s1)
    r_guarded = guarded.step("go", trace=s2)
    assert r_plain.output == r_guarded.output
    assert s1.codes() == s2.codes()  # 审批门未激活任何工具 -> 未发 mw_approval,行为零变化


def test_approved_passes_through():
    gate = AutoApprovalGate(allow=["delete_file"])
    agent = _seeded_agent(gate, gated_tools=["delete_file"], tool_for_step=["delete_file"])
    assert agent.step("go").output == "did-risky-thing"


def test_rejected_short_circuits():
    gate = AutoApprovalGate(deny=["delete_file"])
    agent = _seeded_agent(gate, gated_tools=["delete_file"], tool_for_step=["delete_file"])
    with pytest.raises(ApprovalRejected) as ei:
        agent.step("go")  # 断路:内层 FunctionAgent 不跑
    assert ei.value.code == "approval.rejected" and ei.value.retryable is False


def test_pending_suspends_then_resumes():
    gate = ManualApprovalGate()
    agent = _seeded_agent(gate, gated_tools=["delete_file"], tool_for_step=["delete_file"])
    # 首跑:挂起(request 待审)。
    with pytest.raises(ApprovalPending) as ei:
        agent.step("go")
    request_id = ei.value.context["request_id"]
    assert ei.value.retryable is True
    # out-of-band 批准 -> 铸一次性 resume token -> redeem 授权恢复。
    token = gate.resolve(request_id, Decision.APPROVED)
    assert gate.redeem(token).request_id == request_id
    # 重跑本步:gate 已落决议(review 幂等命中同一 request id)-> 放行,内层执行。
    assert agent.step("go").output == "did-risky-thing"


# ---- 隐私:审批 trace 只记 code / 计数 / 决议,绝不记参数正文 --------------------------------


def test_approval_trace_is_privacy_safe():
    gate = AutoApprovalGate(deny=["delete_file"])
    mw = ApprovalMiddleware(gate, gated_tools=["delete_file"])
    sink = InProcessPrivacyTraceSink()
    ctx = StepContext(agent="g", task=f"删除 {_SENTINEL}", trace=sink, tools=["delete_file"])
    with pytest.raises(ApprovalRejected):
        mw.before_step(ctx)
    assert sink.codes() == ["mw_approval"]
    for event in sink.events:
        assert not {k for k in event.fields if k.strip().lower() in FORBIDDEN_KEYS}
        for value in event.fields.values():
            assert _SENTINEL not in str(value)  # 绝不泄露任务 / 参数正文
