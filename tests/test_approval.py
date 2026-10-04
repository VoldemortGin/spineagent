"""approval 缝的单元测试:审批门三态 + 一次性 resume token + 挂起/恢复 + 零行为变化回归。

conformance 已把「review 返回 Decision + gate 有名 + review 幂等 + 请求摘要不含正文」参数化钉死
(见 test_conformance.py);这里补两个默认门的专属策略语义、resume token 一次性(重放必败)、审批
middleware 的放行/断路/挂起三路、以及默认配置下的零行为变化对照。
"""

import pytest
from corespine.observability.trace import FORBIDDEN_KEYS, InProcessPrivacyTraceSink

from spineagent.agent.approval import (
    ApprovalConflict,
    ApprovalGateError,
    ApprovalMiddleware,
    ApprovalPending,
    ApprovalRejected,
    AutoApprovalGate,
    Decision,
    InMemoryResumeTokenStore,
    InvalidResumeToken,
    ManualApprovalGate,
    approval_gates,
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
        "g", _fc(deleter), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
    )
    # 首跑:挂起(request 待审),工具未执行。
    with pytest.raises(ApprovalPending) as ei:
        agent.step("go")
    request_id = ei.value.context["request_id"]
    assert ei.value.retryable is True
    assert deleter.paths == []
    assert [r.id for r in gate.pending()] == [request_id]
    # out-of-band 批准 -> 铸一次性 resume token -> redeem 授权恢复。
    token = gate.resolve(request_id, Decision.APPROVED)
    assert gate.redeem(token).request_id == request_id
    # 重跑本步:同一工具 + 同一参数命中已落决议 -> 放行,真实工具执行恰好一次。
    assert agent.step("go").output == "finished"
    assert deleter.paths == ["/x"]


def test_approval_binds_to_arguments_not_just_tool_name():
    # 批准只对指纹相同(工具名 + 规范化参数)的调用有效:模型改了参数就必须重新审批。
    deleter = _Deleter()
    gate = ManualApprovalGate()
    mw = ApprovalMiddleware(gate, gated_tools=["delete_file"])
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
