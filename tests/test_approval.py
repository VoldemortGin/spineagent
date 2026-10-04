"""approval 缝的单元测试:审批门三态 + 一次性 resume token + 挂起/恢复 + 零行为变化回归。

conformance 已把「review 返回 Decision + gate 有名 + review 幂等 + 请求摘要不含正文」参数化钉死
(见 test_conformance.py);这里补两个默认门的专属策略语义、resume token 一次性(重放必败)、审批
middleware 的放行/断路/挂起三路、以及默认配置下的零行为变化对照。
"""

import pytest
from corespine.observability.trace import FORBIDDEN_KEYS, InProcessPrivacyTraceSink

from spineagent.agent.approval import (
    ApprovalConfigError,
    ApprovalConflict,
    ApprovalError,
    ApprovalGateError,
    ApprovalMiddleware,
    ApprovalPending,
    ApprovalRejected,
    ApprovalRequest,
    AutoApprovalGate,
    ConsumableApprovalGate,
    Decision,
    InMemoryResumeTokenStore,
    InvalidResumeToken,
    ManualApprovalGate,
    UnknownApprovalRequest,
    approval_gates,
    approval_scope,
    enforce_tool_approval,
    make_approval_gate,
    make_approval_request,
    require_approval,
)
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.agent.middleware import (
    DynamicToolMiddleware,
    MiddlewareAgent,
    middlewares,
)
from spineagent.agent.policy import SyntaxToolPolicy
from spineagent.agent.tool_using import ToolUsingAgent
from spineagent.conformance import ScriptedToolCallProvider
from spineagent.tools.function_tool import FunctionTool
from spineagent.tools.tool import ToolResult

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
    gate.review(req)  # 只能决议已登记的请求
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
    gate.review(req)  # 只能决议已登记的请求
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


# ---- ApprovalMiddleware:放行 / 断路 / 挂起 三路(端到端:真实 FunctionCallingAgent + 真实工具)----
# 旧版这组测试的内层是假执行的 FunctionAgent(只返回一串 "did-risky-thing"),闸只检查 ctx.tools
# 的声明,所以测不出「声明面不含工具 / 重跑步序变了」时真实工具照常执行(fail-open)。现在一律用
# 真实 FunctionCallingAgent + 离线脚本化 provider + 带副作用计数的真实工具函数,断言执行次数。


class _Deleter:
    """带副作用计数的真实工具函数。"""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def __call__(self, path: str) -> str:
        self.paths.append(path)
        return "deleted"


def _delete_tool(deleter: _Deleter) -> FunctionTool:
    return FunctionTool(
        "delete_file",
        "删除文件",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        func=deleter,
    )


def _fc(deleter: _Deleter, path: str = "/x") -> FunctionCallingAgent:
    script = ScriptedToolCallProvider([("delete_file", {"path": path})], final="finished")
    return FunctionCallingAgent("fc", script, [_delete_tool(deleter)])


def test_trigger_no_dynamic_tool_middleware_still_blocks():
    # 触发一(修复前 fail-open):没有 DynamicToolMiddleware 播种 ctx.tools 时,旧闸看不见工具,
    # delete_file 照常执行。现在闸在真实执行点上:执行次数必须为 0。
    deleter = _Deleter()
    gate = AutoApprovalGate(deny=["*"])
    agent = MiddlewareAgent(
        "g", _fc(deleter), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
    )
    with pytest.raises(ApprovalRejected):
        agent.step("go")
    assert deleter.paths == []


def test_trigger_rerun_with_dynamic_tool_does_not_bypass():
    # 触发二(修复前 fail-open):DynamicToolMiddleware({0: [...]}) 只在步序 0 宣告工具,第一次被拒后
    # 原样重跑(步序变 1)即绕过并执行。现在 request id 不依赖步序,重跑多少次都不执行。
    deleter = _Deleter()
    gate = AutoApprovalGate(deny=["*"])
    agent = MiddlewareAgent(
        "g",
        _fc(deleter),
        [
            DynamicToolMiddleware({0: ["delete_file"]}),
            ApprovalMiddleware(gate, gated_tools=["delete_file"]),
        ],
    )
    for _ in range(3):
        with pytest.raises(ApprovalRejected):
            agent.step("go")
    assert deleter.paths == []


def test_default_config_is_zero_behavior_change():
    # 空 gated_tools:同一输入,加不加审批 middleware 输出 + trace code 序列 + 执行次数全等(回归对照)。
    d1, d2 = _Deleter(), _Deleter()
    plain = MiddlewareAgent("g", _fc(d1), [DynamicToolMiddleware(default=("delete_file",))])
    guarded = MiddlewareAgent(
        "g",
        _fc(d2),
        [DynamicToolMiddleware(default=("delete_file",)), ApprovalMiddleware(AutoApprovalGate())],
    )
    s1, s2 = InProcessPrivacyTraceSink(), InProcessPrivacyTraceSink()
    r_plain = plain.step("go", trace=s1)
    r_guarded = guarded.step("go", trace=s2)
    assert r_plain.output == r_guarded.output == "finished"
    assert s1.codes() == s2.codes()  # 未配置审批 -> 未发 mw_approval,行为零变化
    assert d1.paths == d2.paths == ["/x"]


def test_approved_passes_through():
    deleter = _Deleter()
    gate = AutoApprovalGate(allow=["delete_file"])
    agent = MiddlewareAgent(
        "g", _fc(deleter), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
    )
    assert agent.step("go").output == "finished"
    assert deleter.paths == ["/x"]


def test_rejected_short_circuits():
    deleter = _Deleter()
    gate = AutoApprovalGate(deny=["delete_file"])
    agent = MiddlewareAgent(
        "g", _fc(deleter), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
    )
    with pytest.raises(ApprovalRejected) as ei:
        agent.step("go")  # 断路:真实工具不执行
    assert ei.value.code == "approval.rejected" and ei.value.retryable is False
    assert deleter.paths == []


def test_pending_suspends_then_resumes():
    deleter = _Deleter()
    gate = ManualApprovalGate()
    agent = MiddlewareAgent(
        "g", _fc(deleter), [ApprovalMiddleware(gate, gated_tools=["delete_file"], scope="s")]
    )
    # 首跑:挂起(request 待审),工具未执行。
    with pytest.raises(ApprovalPending) as ei:
        agent.step("go")
    request_id = ei.value.context["request_id"]
    assert ei.value.retryable is True
    assert deleter.paths == []
    assert [r.id for r in gate.pending()] == [request_id]
    assert ei.value.context["scope"] == "s"
    # out-of-band 批准 -> 铸一次性 resume token -> redeem 拿回 ticket。
    token = gate.resolve(request_id, Decision.APPROVED)
    ticket = gate.redeem(token)
    assert ticket.request_id == request_id
    # 在同一作用域里重跑本步:同一工具 + 同一参数命中批准 -> 核销后执行恰好一次。
    assert agent.step("go").output == "finished"
    assert deleter.paths == ["/x"]


def test_approval_binds_to_arguments_not_just_tool_name():
    # 批准只对指纹相同(工具名 + 规范化参数)的调用有效:模型改了参数就必须重新审批。
    deleter = _Deleter()
    gate = ManualApprovalGate()
    # 固定作用域:只让「参数不同」成为变量(缺省每次 step 一个新作用域,会掩盖本测试要钉的东西)。
    mw = ApprovalMiddleware(gate, gated_tools=["delete_file"], scope="session")
    with pytest.raises(ApprovalPending) as ei:
        MiddlewareAgent("g", _fc(deleter, "/safe"), [mw]).step("go")
    gate.resolve(ei.value.context["request_id"], Decision.APPROVED)
    with pytest.raises(ApprovalPending):
        MiddlewareAgent("g", _fc(deleter, "/etc"), [mw]).step("go")
    assert deleter.paths == []


def test_gate_exception_fails_closed():
    class Broken:
        name = "broken"

        def review(self, request):
            raise RuntimeError("审批后端挂了")

    deleter = _Deleter()
    agent = MiddlewareAgent(
        "g", _fc(deleter), [ApprovalMiddleware(Broken(), gated_tools=["delete_file"])]
    )
    with pytest.raises(ApprovalGateError) as ei:
        agent.step("go")
    assert ei.value.code == "approval.gate_error"
    assert deleter.paths == []


def test_gate_returning_non_decision_fails_closed():
    class Sloppy:
        name = "sloppy"

        def review(self, request):
            return "yes"  # 不是 Decision:绝不当作放行

    deleter = _Deleter()
    agent = MiddlewareAgent(
        "g", _fc(deleter), [ApprovalMiddleware(Sloppy(), gated_tools=["delete_file"])]
    )
    with pytest.raises(ApprovalGateError):
        agent.step("go")
    assert deleter.paths == []


def test_guard_scope_is_released_after_step_even_on_error():
    # 审批作用域只覆盖被包裹的那一步:步内抛错后作用域照样释放,不污染之后的无审批调用。
    deleter = _Deleter()
    gate = AutoApprovalGate(deny=["*"])
    with pytest.raises(ApprovalRejected):
        MiddlewareAgent(
            "g", _fc(deleter), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
        ).step("go")
    assert _fc(deleter).step("go").output == "finished"  # 无审批配置的 agent:照常执行
    assert deleter.paths == ["/x"]


def test_enforce_without_any_configuration_is_noop():
    enforce_tool_approval("delete_file", {"path": "/x"})  # 未配置审批:零行为变化,不抛


def test_require_approval_wraps_plain_tool():
    class Nuke:
        name = "nuke"

        def __init__(self) -> None:
            self.hits = 0

        def run(self, arg):
            self.hits += 1
            return ToolResult(tool=self.name, output="boom")

    nuke = Nuke()
    gated = require_approval(nuke, AutoApprovalGate(deny=["nuke"]))
    assert gated.name == "nuke"
    agent = ToolUsingAgent("tu", SyntaxToolPolicy(), [gated])
    with pytest.raises(ApprovalRejected):
        agent.step("nuke: now")
    assert nuke.hits == 0


def test_make_approval_request_bind_values():
    a = make_approval_request("tool_call", "rm", {"path": "/a"}, bind_values=True)
    b = make_approval_request("tool_call", "rm", {"path": "/b"}, bind_values=True)
    a2 = make_approval_request("tool_call", "rm", {"path": "/a"}, bind_values=True)
    assert a.id != b.id and a.id == a2.id
    assert a.arg_fingerprint == b.arg_fingerprint  # schema 指纹仍只覆盖键名 + 值类型
    assert "/a" not in (a.code + a.id + a.tool + a.arg_fingerprint)


# ---- 隐私:审批 trace 只记 code / 计数 / 决议,绝不记参数正文 --------------------------------


def test_approval_trace_is_privacy_safe():
    deleter = _Deleter()
    gate = AutoApprovalGate(deny=["delete_file"])
    agent = MiddlewareAgent(
        "g", _fc(deleter, _SENTINEL), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
    )
    sink = InProcessPrivacyTraceSink()
    with pytest.raises(ApprovalRejected):
        agent.step(f"删除 {_SENTINEL}", trace=sink)
    assert "mw_approval" in sink.codes()
    for event in sink.events:
        assert not {k for k in event.fields if k.strip().lower() in FORBIDDEN_KEYS}
        for value in event.fields.values():
            assert _SENTINEL not in str(value)  # 绝不泄露任务 / 参数正文


# ---- 审查复现(修复 2):批准语义 —— 一次性消费 / 作用域 / 只能决议已存在的请求 ----------------------


def _fc_calls(deleter: _Deleter, calls: int, path: str = "/a", name: str = "fc"):
    script = ScriptedToolCallProvider([("delete_file", {"path": path})] * calls, final="finished")
    return FunctionCallingAgent(name, script, [_delete_tool(deleter)])


def _guarded(agent, gate, scope="s", **kw):
    return MiddlewareAgent(
        "mw", agent, [ApprovalMiddleware(gate, gated_tools=["delete_file"], scope=scope, **kw)]
    )


def test_review_a1_one_approval_never_buys_unlimited_executions():
    # 审查 A1:只 resolve 一次、从不 redeem,之后同一步里重复 6 次、换一个 agent 再 6 次,共执行了 12 次。
    deleter, gate = _Deleter(), ManualApprovalGate()
    with pytest.raises(ApprovalPending) as ei:
        _guarded(_fc_calls(deleter, 1), gate).step("x")
    gate.resolve(ei.value.context["request_id"], Decision.APPROVED)
    for name in ("fc", "other-agent"):
        with pytest.raises(ApprovalPending):
            _guarded(_fc_calls(deleter, 6, name=name), gate).step("again")
    assert len(deleter.paths) <= 1


def test_review_resolve_cannot_preapprove_an_offline_computed_id():
    # 审查:request id 只由 code + 工具名 + 参数派生,resolve 不要求请求被 review 过 —— 可离线算出 id 预先批准。
    gate = ManualApprovalGate()
    forged = make_approval_request("tool_call", "delete_file", {"path": "/a"}, bind_values=True)
    with pytest.raises(ApprovalError):
        gate.resolve(forged.id, Decision.APPROVED)
    deleter = _Deleter()
    with pytest.raises(ApprovalPending):
        _guarded(_fc_calls(deleter, 1), gate).step("x")
    assert deleter.paths == []


def test_review_a11_approver_sees_what_is_being_approved():
    # 审查 A11:审批人经 pending() 只看到工具名与 schema 指纹 —— transfer(1) 与 transfer(1000000) 一模一样。
    gate = ManualApprovalGate()
    for amount in ("1", "1000000"):
        with pytest.raises(ApprovalPending):
            _guarded(_fc_calls(_Deleter(), 1, path=amount), gate).step("t")
    previews = {dict(r.preview)["path"] for r in gate.pending()}
    assert previews == {"'1'", "'1000000'"} or previews == {"1", "1000000"}


def test_review_a6_misspelled_gated_name_fails_closed():
    # 审查 A6:gated_tools=["Delete_File"](大小写写错)静默放行。
    deleter = _Deleter()
    agent = MiddlewareAgent(
        "mw",
        _fc_calls(deleter, 1),
        [ApprovalMiddleware(AutoApprovalGate(deny=["*"]), gated_tools=["Delete_File"])],
    )
    with pytest.raises(ApprovalError):
        agent.step("t")
    assert deleter.paths == []


def test_review_glob_gated_name_is_refused():
    with pytest.raises(ValueError):
        ApprovalMiddleware(AutoApprovalGate(deny=["*"]), gated_tools=["delete_*"])


# ---- 修复 2:新语义的细节(作用域 / 额度 / 生命周期 / 预览 / ticket / 名字校验)-------------------


def _pending_id(agent, task="t"):
    with pytest.raises(ApprovalPending) as ei:
        agent.step(task)
    return ei.value


def test_shared_gate_does_not_leak_between_explicit_scopes():
    deleter, gate = _Deleter(), ManualApprovalGate()
    alice = _guarded(_fc_calls(deleter, 1), gate, scope="alice")
    bob = _guarded(_fc_calls(deleter, 1), gate, scope="bob")
    gate.resolve(_pending_id(alice).context["request_id"], Decision.APPROVED)
    _pending_id(bob)
    assert deleter.paths == []
    alice.step("t")
    assert deleter.paths == ["/a"]


def test_uses_n_and_reusable_mode():
    deleter, gate = _Deleter(), ManualApprovalGate()
    agent = _guarded(_fc_calls(deleter, 3), gate, scope="s")
    gate.resolve(_pending_id(agent).context["request_id"], Decision.APPROVED, uses=2)
    _pending_id(agent)  # 第三次调用额度用尽
    assert deleter.paths == ["/a", "/a"]

    deleter2, gate2 = _Deleter(), ManualApprovalGate()
    agent2 = _guarded(_fc_calls(deleter2, 3), gate2, scope="s")
    # 显式可选的幂等模式:有效期内不限次(风险见 ADR 0002)。
    gate2.resolve(_pending_id(agent2).context["request_id"], Decision.APPROVED, uses=None)
    agent2.step("t")
    agent2.step("t")
    assert len(deleter2.paths) == 6


def test_resolve_validates_uses_and_ttl():
    gate = ManualApprovalGate()
    req = make_approval_request("tool_call", "rm")
    gate.review(req)
    with pytest.raises(ValueError):
        gate.resolve(req.id, Decision.APPROVED, uses=0)
    with pytest.raises(ValueError):
        gate.resolve(req.id, Decision.APPROVED, ttl_seconds=0)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_pending_table_is_bounded():
    gate = ManualApprovalGate(max_requests=3)
    ids = []
    for i in range(5):
        req = make_approval_request("tool_call", "rm", {"i": i}, bind_values=True)
        gate.review(req)
        ids.append(req.id)
    assert {r.id for r in gate.pending()} == set(ids[2:])  # 最早登记的被淘汰
    with pytest.raises(UnknownApprovalRequest):
        gate.resolve(ids[0], Decision.APPROVED)


def test_requests_and_approvals_expire():
    clock = _Clock()
    gate = ManualApprovalGate(request_ttl=10.0, now_fn=clock)
    req = make_approval_request("tool_call", "rm", {"p": "/a"}, bind_values=True)
    gate.review(req)
    clock.now = 11.0
    assert gate.pending() == []
    with pytest.raises(UnknownApprovalRequest):
        gate.resolve(req.id, Decision.APPROVED)
    assert gate.review(req) is Decision.PENDING  # 过期后重新登记
    gate.resolve(req.id, Decision.APPROVED, ttl_seconds=5.0)
    clock.now = 17.0
    assert gate.review(req) is Decision.PENDING  # 批准过期,不再放行
    assert gate.consume(req) is False


def test_rejection_is_not_consumed():
    deleter, gate = _Deleter(), ManualApprovalGate()
    agent = _guarded(_fc_calls(deleter, 1), gate, scope="s")
    gate.resolve(_pending_id(agent).context["request_id"], Decision.REJECTED)
    for _ in range(2):
        with pytest.raises(ApprovalRejected):
            agent.step("t")
    assert deleter.paths == []


def test_preview_is_redacted_truncated_and_never_traced():
    calls = [
        ("transfer", {"amount": 1000000, "password": "hunter2-SENTINEL", "memo": "x" * 500}),
    ]
    hits: list[dict] = []

    def transfer(amount: int, password: str, memo: str) -> str:
        hits.append({"amount": amount})
        return "ok"

    tool = FunctionTool(
        "transfer",
        "",
        {
            "type": "object",
            "properties": {
                "amount": {"type": "integer"},
                "password": {"type": "string"},
                "memo": {"type": "string"},
            },
        },
        func=transfer,
    )
    gate = ManualApprovalGate()
    agent = MiddlewareAgent(
        "mw",
        FunctionCallingAgent("fc", ScriptedToolCallProvider(calls), [tool]),
        [ApprovalMiddleware(gate, gated_tools=["transfer"], scope="s")],
    )
    sink = InProcessPrivacyTraceSink()
    with pytest.raises(ApprovalPending):
        agent.step("t", trace=sink)
    [request] = gate.pending()
    preview = dict(request.preview)
    assert preview["amount"] == "1000000"  # 审批人看得到要批的是什么
    assert preview["password"] == "***"  # 敏感键打码
    assert len(preview["memo"]) < 260 and preview["memo"].startswith("'xxx")  # 截断
    assert "hunter2" not in repr(request)  # 预览不进 repr
    for event in sink.events:
        for value in event.fields.values():
            assert "hunter2" not in str(value) and "1000000" not in str(value)
    assert hits == []


def test_custom_redactor_and_failing_redactor():
    gate = ManualApprovalGate()

    def hide_all(key, value):
        return "<hidden>"

    agent = _guarded(_fc_calls(_Deleter(), 1, path="/secret"), gate, redact=hide_all)
    _pending_id(agent)
    assert dict(gate.pending()[0].preview) == {"path": "<hidden>"}

    def broken(key, value):
        raise RuntimeError("boom")

    gate2 = ManualApprovalGate()
    _pending_id(_guarded(_fc_calls(_Deleter(), 1, path="/secret"), gate2, redact=broken))
    assert dict(gate2.pending()[0].preview) == {"path": "***"}  # 钩子出错整值打码,不回退原文


def test_ticket_replay_cannot_execute_twice():
    deleter, gate = _Deleter(), ManualApprovalGate()
    agent = _guarded(_fc_calls(deleter, 1), gate)
    pending = _pending_id(agent)
    gate.redeem(gate.resolve(pending.context["request_id"], Decision.APPROVED))
    agent.step("t")
    with pytest.raises(ApprovalPending):
        agent.step("t")  # 持有 ticket 不等于持有放行额度:批准已核销
    assert deleter.paths == ["/a"]


def test_stacked_static_and_dynamic_gate_consume_once():
    # require_approval 与 ApprovalMiddleware 用同一个门叠加:同一调用只审 / 核销一次。
    deleter, gate = _Deleter(), ManualApprovalGate()
    tool = require_approval(_delete_tool(deleter), gate)
    script = ScriptedToolCallProvider([("delete_file", {"path": "/a"})], final="finished")
    agent = _guarded(FunctionCallingAgent("fc", script, [tool]), gate, scope="s")
    gate.resolve(_pending_id(agent).context["request_id"], Decision.APPROVED)
    assert agent.step("t").output == "finished"
    assert deleter.paths == ["/a"]


def test_nested_middlewares_with_same_gate_share_scope_and_consume_once():
    deleter, gate = _Deleter(), ManualApprovalGate()
    inner = MiddlewareAgent(
        "inner", _fc_calls(deleter, 1), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
    )
    outer = _guarded(inner, gate, scope="outer-session")
    pending = _pending_id(outer)
    assert len(gate.pending()) == 1  # 嵌套共享外层作用域:一次调用只产生一个待审请求
    assert pending.context["scope"] == "outer-session"
    gate.resolve(pending.context["request_id"], Decision.APPROVED)
    outer.step("t")
    assert deleter.paths == ["/a"]


def test_unknown_gated_name_fails_closed_when_inventory_is_known():
    deleter = _Deleter()
    agent = MiddlewareAgent(
        "mw",
        _fc_calls(deleter, 1),
        [ApprovalMiddleware(AutoApprovalGate(deny=["*"]), gated_tools=["rm_rf"])],
    )
    with pytest.raises(ApprovalConfigError):
        agent.step("t")
    assert deleter.paths == []


def test_misspelled_gated_name_fails_closed_for_opaque_and_tool_using_agents():
    from spineagent.agent.agent import FunctionAgent

    deleter = _Deleter()
    inner = _fc_calls(deleter, 1)
    opaque = FunctionAgent("opaque", lambda t: inner.step(t).output)  # 推断不了工具清单
    gate = AutoApprovalGate(deny=["*"])
    with pytest.raises(ApprovalConfigError):
        MiddlewareAgent("mw", opaque, [ApprovalMiddleware(gate, gated_tools=["DELETE-file"])]).step(
            "t"
        )
    assert deleter.paths == []

    hits: list[str] = []

    class Nuke:
        name = "nuke_db"

        def run(self, arg):
            hits.append(arg)
            return ToolResult(tool=self.name, output="boom")

    tu = ToolUsingAgent("tu", SyntaxToolPolicy(), [Nuke()])
    with pytest.raises(ApprovalConfigError):
        MiddlewareAgent("mw", tu, [ApprovalMiddleware(gate, gated_tools=["Nuke_DB"])]).step(
            "nuke_db: now"
        )
    assert hits == []


def test_invalid_gated_names_are_refused_at_construction():
    for bad in (["delete_?"], ["[a-z]"], [""], ["*"]):
        with pytest.raises(ApprovalConfigError):
            ApprovalMiddleware(AutoApprovalGate(), gated_tools=bad)
    with pytest.raises(ValueError):
        ApprovalMiddleware(AutoApprovalGate(), gated_tools=["x"], scope="")


def test_consumable_protocol():
    assert isinstance(ManualApprovalGate(), ConsumableApprovalGate)
    assert not isinstance(AutoApprovalGate(), ConsumableApprovalGate)


# ---- 第三轮修改 2:作用域必须显式,不再有隐式缺省 --------------------------------------------------


def test_review_r2_pending_gate_without_scope_is_a_config_error_before_anything_runs():
    # 复审 E:缺省作用域每次 step 新建 -> 捕获 pending -> resolve -> 原样重跑,三次得到三个不同的 request id,
    # 执行 0 次,收件箱里堆着永远批不掉的请求。现在:会产生待审请求的门没有显式作用域 -> 明确的配置错误。
    deleter, gate = _Deleter(), ManualApprovalGate()
    agent = MiddlewareAgent(
        "mw", _fc_calls(deleter, 1), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
    )
    with pytest.raises(ApprovalConfigError) as ei:
        agent.step("t")
    assert "scope=" in str(ei.value)
    assert deleter.paths == [] and gate.pending() == []


def test_review_r2_explicit_scope_flow_completes_and_executes_exactly_once():
    deleter, gate = _Deleter(), ManualApprovalGate()
    agent = MiddlewareAgent(
        "mw",
        _fc_calls(deleter, 1),
        [ApprovalMiddleware(gate, gated_tools=["delete_file"], scope="tenant-1:chat-9")],
    )
    ids = {_pending_id(agent).context["request_id"] for _ in range(3)}
    assert len(ids) == 1 and len(gate.pending()) == 1
    gate.resolve(ids.pop(), Decision.APPROVED)
    assert agent.step("t").output == "finished"
    assert deleter.paths == ["/a"]
    _pending_id(agent)  # 批准已核销
    assert deleter.paths == ["/a"]


def test_outer_approval_scope_counts_as_explicit():
    deleter, gate = _Deleter(), ManualApprovalGate()
    agent = MiddlewareAgent(
        "mw", _fc_calls(deleter, 1), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
    )
    with approval_scope("tenant-1:chat-9"):
        pending = _pending_id(agent)
    assert pending.context["scope"] == "tenant-1:chat-9"


def test_synchronous_gates_need_no_scope():
    for gate, expect in (
        (AutoApprovalGate(allow=["delete_file"]), ["/a"]),
        (AutoApprovalGate(deny=["delete_file"]), []),
    ):
        deleter = _Deleter()
        agent = MiddlewareAgent(
            "mw", _fc_calls(deleter, 1), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
        )
        try:
            agent.step("t")
        except ApprovalRejected:
            pass
        assert deleter.paths == expect


def test_non_consumable_gate_that_pends_without_scope_is_a_config_error():
    class External:
        """第三方门:不实现 consume,但会返回 PENDING(例如转发给外部审批系统)。"""

        name = "external"

        def review(self, request):
            return Decision.PENDING

    deleter = _Deleter()
    agent = MiddlewareAgent(
        "mw", _fc_calls(deleter, 1), [ApprovalMiddleware(External(), gated_tools=["delete_file"])]
    )
    with pytest.raises(ApprovalConfigError):
        agent.step("t")
    with approval_scope("s"), pytest.raises(ApprovalPending):
        agent.step("t")
    assert deleter.paths == []


def test_review_r2_shared_require_approval_tool_needs_a_scope_per_caller():
    # 复审 RA:require_approval 的缺省作用域按包装实例生成——模块级共享的工具对所有用户是同一个作用域,
    # Bob 用 Alice 的批准执行了同参调用。
    deleter, gate = _Deleter(), ManualApprovalGate()
    shared = require_approval(_delete_tool(deleter), gate)
    with pytest.raises(ApprovalConfigError):
        _fc_calls_with(shared).step("alice")
    assert gate.pending() == []
    with approval_scope("tenant-alice"):
        alice = _pending_id(_fc_calls_with(shared))
    gate.resolve(alice.context["request_id"], Decision.APPROVED)
    with approval_scope("tenant-bob"):
        _pending_id(_fc_calls_with(shared))
    assert deleter.paths == []
    with approval_scope("tenant-alice"):
        _fc_calls_with(shared).step("alice")
    assert deleter.paths == ["/a"]


def _fc_calls_with(tool, path="/a"):
    script = ScriptedToolCallProvider([("delete_file", {"path": path})], final="finished")
    return FunctionCallingAgent("fc", script, [tool])


def test_review_r2_feed_back_mode_hands_request_and_scope_to_the_caller_not_the_model():
    # 复审 F:feed_back 模式下调用方根本拿不到作用域(也就无法完成审批流程)。
    seen: list[str] = []

    class Recording(ScriptedToolCallProvider):
        def chat(self, messages, *, tools=None):
            seen[:] = [m["content"] for m in messages if m.get("role") == "tool"]
            return super().chat(messages, tools=tools)

    deleter, gate = _Deleter(), ManualApprovalGate()
    fc = FunctionCallingAgent(
        "fc",
        Recording([("delete_file", {"path": "/a"})], final="asked"),
        [_delete_tool(deleter)],
        on_approval="feed_back",
    )
    agent = MiddlewareAgent(
        "mw", fc, [ApprovalMiddleware(gate, gated_tools=["delete_file"], scope="tenant-7")]
    )
    result = agent.step("t")
    [held] = result.held_approvals
    [request] = gate.pending()
    assert held == {
        "code": "approval.pending",
        "request_id": request.id,
        "scope": "tenant-7",
        "tool": "delete_file",
    }
    assert request.id in seen[0] and "tenant-7" not in seen[0]  # 模型看不到作用域
    gate.resolve(held["request_id"], Decision.APPROVED)
    assert agent.step("t").held_approvals == ()
    assert deleter.paths == ["/a"]


def test_consume_only_matches_the_registered_request():
    # 批准绑定到登记时的请求对象:id 相同但其它字段不同的请求不能核销它。
    gate = ManualApprovalGate()
    real = make_approval_request("tool_call", "rm", {"p": "/a"}, bind_values=True, scope="alice")
    gate.review(real)
    gate.resolve(real.id, Decision.APPROVED)
    forged = ApprovalRequest(code=real.code, id=real.id, tool=real.tool, scope="bob")
    assert gate.consume(forged) is False
    assert gate.consume(real) is True
