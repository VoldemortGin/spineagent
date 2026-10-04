"""approval 缝:审批门(ApprovalGate)—— agent 触及风险动作前暂停,等人类批准 / 拒绝后恢复。

对标 n8n Wait 节点与 Send-and-Wait 审批的【概念】(n8n 是 fair-code 许可,只学概念、绝不抄代码):
把家族从「一路跑到底的 agent」扩成「能在风险动作前停下等人拍板」的系统。本批只做框架机制;
产品侧的审批收件箱 UI 留给 spinestudio 后续批。

家族缝的元模式(同 trigger / credential / sandbox 缝):**Protocol + 离线确定性默认 + Registry
工厂 + 参数化 conformance**。一次「待审批请求」只携带 domain-neutral 的【定位摘要】——审批类别
code、确定性 request id、触发的工具名、参数 schema 指纹与计数——【绝不含参数正文 / 值】。gate 对
它给出三态决议之一:approved / rejected / pending。

两个离线确定性默认:
  - AutoApprovalGate —— 策略表:按工具名 glob 模式 allow / deny,纯配置、确定性,永不 pending;
  - ManualApprovalGate —— 进程内挂起:review 登记待审、显式 resolve(request_id, decision) 落决议并
    铸一枚【一次性 resume token】(secrets 随机、只存 sha256 哈希、用过即废,模式照 spinestudio
    refresh_store,但这里是框架内存 / 可插拔 ResumeTokenStore)。

【幂等性裁决(钉死)】决议是 request id 的稳定函数:review 同一请求恒返回同一决议(未决则恒
PENDING);resolve 首次落决议,重复以【相同】决议 resolve 幂等(各自铸一枚独立一次性 token),
以【冲突】决议 resolve 抛 ApprovalConflict(先落者胜,绝不静默翻转)。

【resume token 一次性裁决(钉死)】token 明文只在 resolve 那一刻返回一次,落库只存 sha256 哈希;
redeem 校验 + 标记消费,重放(再次 redeem 同一 token)必抛 InvalidResumeToken——泄露的 token 也
无从二次恢复。

【执行闸裁决(钉死,见 docs/adr/0002)】审批是【工具调用点上的强制闸】,不是对 ctx.tools 声明的
检查:执行点在每一次真实调用工具前调 enforce_tool_approval(真实工具名, 参数);request id 由
(code, 工具名, schema 指纹, 规范化参数值的 sha256)派生,不依赖步序;门故障 fail-closed。

隐私:本缝实现【自身不发射 trace】(同 credential / trigger 缝);request 只带定位摘要,payload
正文到不了任何字段。要观测在【审批 middleware】里记 code / 计数 / 决议,绝不记参数正文。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol, overload, runtime_checkable

from corespine.errors import CorespineError
from corespine.observability.trace import TraceSink
from corespine.seam.registry import Registry

from spineagent.agent.agent import AgentResult
from spineagent.agent.middleware import StepContext, middlewares
from spineagent.tools.function_tool import FunctionTool
from spineagent.tools.tool import Tool, ToolResult

# 内置审批类别 code(domain-neutral;调用方用它路由 / 计数)。
APPROVAL_TOOL_CALL = "tool_call"


class Decision(StrEnum):
    """审批三态决议:放行 / 拒绝 / 待定(继承 str 便于落库 / 记 trace / 序列化)。"""

    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING = "pending"


# ---- 边界异常(带稳定可 grep 的 code,经 corespine.error_to_dict 可归一进 AgentResult.error)----


class ApprovalError(CorespineError):
    """approval 缝的边界异常基类。"""

    code = "approval.error"


class ApprovalRejected(ApprovalError):
    """审批被拒:风险动作断路。不可重试(人类已明确说不)。"""

    code = "approval.rejected"
    retryable = False


class ApprovalPending(ApprovalError):
    """审批待定:run 挂起,等 out-of-band resolve 后凭 resume token 重跑。可重试(等人拍板)。

    request_id 收进 context,供调用方 / 编排层据它 resolve(与后续 spinestudio 审批收件箱对接)。
    """

    code = "approval.pending"
    retryable = True


class InvalidResumeToken(ApprovalError):
    """resume token 不存在 / 已被消费(重放),一律拒绝恢复。"""

    code = "approval.invalid_resume_token"


class ApprovalConflict(ApprovalError):
    """对同一 request 以冲突决议二次 resolve(先落者胜,绝不静默翻转)。"""

    code = "approval.conflict"


class ApprovalGateError(ApprovalError):
    """审批门自身故障(review 抛异常 / 返回非 Decision):fail-closed,受审批工具一律不执行。"""

    code = "approval.gate_error"


# ---- 请求摘要 + 确定性派生(绝不含参数正文)-----------------------------------------------------


@dataclass(frozen=True)
class ApprovalRequest:
    """一次待审批请求(只读):审批类别 code + 确定性 request id + 定位摘要(绝不含参数正文)。

    - code:审批类别 code(如 "tool_call";调用方据它路由 / 计数);
    - id:确定性且唯一的 request id(纯函数派生:同一逻辑动作恒同 id,可重放——正是它让挂起的
      run 在 resolve 后重跑 step 能稳定命中已落决议);
    - tool:触发审批的工具名(定位符,非正文);
    - arg_fingerprint:参数的 schema 指纹(sha256 前 16 位,只覆盖【键名 + 值类型】,绝不含值);
    - arg_count:参数个数(计数)。
    """

    code: str
    id: str
    tool: str
    arg_fingerprint: str = ""
    arg_count: int = 0


def _fingerprint(args: Mapping[str, object]) -> str:
    """据参数 schema 形状(键名 + 值类型)派生确定性指纹——绝不含任何值 / 正文。"""
    shape = sorted([str(k), type(v).__name__] for k, v in args.items())
    raw = json.dumps(shape, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _value_digest(args: Mapping[str, object]) -> str:
    """规范化参数(键排序 + 紧凑 JSON)后取 sha256——只用作 request id 的派生材料,绝不落明文。

    非 JSON 值退回 repr:它若不稳定,只会让请求「对不上旧批准」而要求重新审批(偏向 fail-closed)。
    """
    raw = json.dumps(
        {str(k): v for k, v in args.items()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _request_id(code: str, tool: str, fingerprint: str, nonce: str) -> str:
    """据 code + 工具名 + schema 指纹 + nonce 派生确定性 request id(纯函数,可重放)。"""
    raw = ":".join([code, tool, fingerprint, nonce])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def make_approval_request(
    code: str,
    tool: str,
    arguments: Mapping[str, object] | None = None,
    *,
    nonce: str = "",
    bind_values: bool = False,
) -> ApprovalRequest:
    """从工具名 + 参数字典构造一次待审批请求(只取 schema 指纹与计数,绝不落参数值)。

    nonce 让调用方在需要「按次审批」时强制区分 schema 相同的两次调用;缺省 "" 时 schema 相同的
    请求折叠成同一 id(决议可复用,幂等)。bind_values=True 时把【规范化参数值的 sha256】也折进
    request id(仍不落明文):参数一变即是新请求、须重新审批——工具调用点上的执行闸用的就是它。
    """
    args = dict(arguments or {})
    fp = _fingerprint(args)
    salt = nonce if not bind_values else f"{nonce}:{_value_digest(args)}"
    return ApprovalRequest(
        code=code,
        id=_request_id(code, tool, fp, salt),
        tool=tool,
        arg_fingerprint=fp,
        arg_count=len(args),
    )


# ---- ApprovalGate 协议 ------------------------------------------------------------------------


@runtime_checkable
class ApprovalGate(Protocol):
    """审批门协议:有名字;对一次待审批请求给出三态决议。

    契约(由 conformance 钉死):review 返回 Decision;对同一 request 幂等(重复 review 恒同决议,
    除非其间发生了 resolve)。实现自身不发 trace——请求只带定位摘要,payload 正文无从泄漏。
    """

    name: str

    def review(self, request: ApprovalRequest) -> Decision: ...


# ---- 一次性 resume token 存储(可插拔;框架内存默认)------------------------------------------


@dataclass(frozen=True)
class ResumeTicket:
    """一次成功 redeem 的凭据:指向被授权恢复的 request 及其决议(供调用方据此重跑 step)。"""

    request_id: str
    decision: Decision


@runtime_checkable
class ResumeTokenStore(Protocol):
    """一次性 resume token 存储的最小接口:签发(明文只此一次)+ 消费(重放必败)。"""

    def issue(self, request_id: str, decision: Decision) -> str: ...

    def redeem(self, raw_token: str) -> ResumeTicket: ...


def _hash_token(raw_token: str) -> str:
    """对不透明 token 取 sha256 十六进制哈希(只存哈希,绝不存明文)。"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@dataclass
class _TokenRecord:
    request_id: str
    decision: Decision
    consumed: bool = False


class InMemoryResumeTokenStore:
    """进程内一次性 token 存储(框架默认):明文只在 issue 返回一次,落表只存 sha256 哈希。

    redeem 校验哈希 + 标记 consumed,重放(再次 redeem 同一 token)抛 InvalidResumeToken。零落地、
    离线确定性(随机来自 secrets,故 token 本身不可预测,但一次性 / 重放语义完全可测)。repr 只暴露
    记录【计数】,绝不暴露任何 token / request_id。
    """

    def __init__(self) -> None:
        self._records: dict[str, _TokenRecord] = {}

    def issue(self, request_id: str, decision: Decision) -> str:
        raw_token = secrets.token_urlsafe(32)
        self._records[_hash_token(raw_token)] = _TokenRecord(request_id, decision)
        return raw_token

    def redeem(self, raw_token: str) -> ResumeTicket:
        record = self._records.get(_hash_token(raw_token))
        if record is None:
            raise InvalidResumeToken("resume token 无效")
        if record.consumed:
            raise InvalidResumeToken("resume token 已被消费(重放)")
        self._records[_hash_token(raw_token)] = replace(record, consumed=True)
        return ResumeTicket(request_id=record.request_id, decision=record.decision)

    def __repr__(self) -> str:
        return f"InMemoryResumeTokenStore(records={len(self._records)})"


# ---- 离线确定性默认实现 ------------------------------------------------------------------------


class AutoApprovalGate:
    """策略表审批门:按工具名 glob 模式 allow / deny,纯配置、确定性,永不 pending。

    优先级:先撞 deny 模式 -> REJECTED;再撞 allow 模式 -> APPROVED;都不撞 -> default(缺省
    APPROVED,即「默认放行」)。是「自动化 / 无人值守」路径的参照实现:决议只是请求的纯函数,天然
    幂等、可重放。repr 只暴露模式【计数】。
    """

    name = "auto"

    def __init__(
        self,
        *,
        allow: Sequence[str] = (),
        deny: Sequence[str] = (),
        default: Decision = Decision.APPROVED,
    ) -> None:
        if default is Decision.PENDING:
            raise ValueError("AutoApprovalGate 的 default 不能是 PENDING(它永不挂起)")
        self._allow = tuple(allow)
        self._deny = tuple(deny)
        self._default = default

    def review(self, request: ApprovalRequest) -> Decision:
        if any(fnmatch.fnmatchcase(request.tool, pat) for pat in self._deny):
            return Decision.REJECTED
        if any(fnmatch.fnmatchcase(request.tool, pat) for pat in self._allow):
            return Decision.APPROVED
        return self._default

    def __repr__(self) -> str:
        return (
            f"AutoApprovalGate(allow={len(self._allow)}, deny={len(self._deny)}, "
            f"default={self._default.value})"
        )


class ManualApprovalGate:
    """进程内挂起审批门:review 登记待审并返回当前决议,resolve 落决议 + 铸一次性 resume token。

    review(request):首见即登记为待审(供 pending() 列举,对接审批收件箱);返回已落决议或 PENDING。
    resolve(request_id, decision):把 approved / rejected 决议落进 gate,并铸一枚一次性 resume token
    返回(明文只此一次)。redeem(raw_token):消费 token(重放必败),拿回 ResumeTicket 以授权恢复。

    幂等:review 对同一 request 恒返回同一决议;resolve 以相同决议重复调用幂等(各铸独立 token),
    以冲突决议调用抛 ApprovalConflict。token 存储可注入(默认进程内一次性),便于换持久后端。
    """

    name = "manual"

    def __init__(self, *, token_store: ResumeTokenStore | None = None) -> None:
        self._registry: dict[str, ApprovalRequest] = {}
        self._decisions: dict[str, Decision] = {}
        self._store: ResumeTokenStore = token_store or InMemoryResumeTokenStore()

    def review(self, request: ApprovalRequest) -> Decision:
        self._registry.setdefault(request.id, request)  # 登记待审(幂等)
        return self._decisions.get(request.id, Decision.PENDING)

    def resolve(self, request_id: str, decision: Decision) -> str:
        """落一个 approved / rejected 决议并铸一枚一次性 resume token(明文只此一次)。"""
        if decision not in (Decision.APPROVED, Decision.REJECTED):
            raise ValueError(f"resolve 只接受 approved / rejected,收到:{decision!r}")
        prior = self._decisions.get(request_id)
        if prior is not None and prior is not decision:
            raise ApprovalConflict(
                f"request {request_id!r} 已落决议 {prior.value!r},不接受冲突决议 {decision.value!r}",
                request_id=request_id,
            )
        self._decisions[request_id] = decision
        return self._store.issue(request_id, decision)

    def redeem(self, raw_token: str) -> ResumeTicket:
        """消费一枚一次性 resume token(重放必败),拿回被授权恢复的 request 与决议。"""
        return self._store.redeem(raw_token)

    def pending(self) -> list[ApprovalRequest]:
        """列举尚未落决议的待审请求(按 id 字典序;只含定位摘要,供审批收件箱读取)。"""
        return [req for rid, req in sorted(self._registry.items()) if rid not in self._decisions]

    def __repr__(self) -> str:
        undecided = sum(1 for rid in self._registry if rid not in self._decisions)
        return f"ManualApprovalGate(seen={len(self._registry)}, pending={undecided})"


# ---- Registry + 工厂 --------------------------------------------------------------------------

# approval-gate 缝的注册表:内置 auto / manual;真实后端(如接外部审批系统)经 entry-point group
# "corespine.approval_gate" 自动发现(第三方装包即扩展,核心不实现任何外部客户端)。
approval_gates: Registry[ApprovalGate] = Registry("approval_gate")
approval_gates.register("auto", lambda **kw: AutoApprovalGate(**kw))
approval_gates.register("manual", lambda **kw: ManualApprovalGate(**kw))


def make_approval_gate(spec: str, **kwargs: object) -> ApprovalGate:
    """按 spec 构造一个 ApprovalGate(大小写 / 连字符 / 留白不敏感)。

    内置:"auto"(可选 allow=/deny=/default=)、"manual"(可选 token_store=);未知 spec 抛
    ValueError 并列清可用名。真实后端由第三方经 entry-point 注册后,同样以 spec 名选用。
    """
    return approval_gates.make(spec, **kwargs)


# ---- 工具调用点上的执行闸 --------------------------------------------------------------------
#
# 审批不是对 ctx.tools 声明的检查,而是【真实执行点】上的强制闸:FunctionCallingAgent / ToolUsingAgent
# 在每一次真正调用工具函数之前调 enforce_tool_approval(真实工具名, 真实参数)。门经两条通道到达执行点:
#   1. 动态作用域(ApprovalMiddleware):before_step 把一枚 _ApprovalGuard 压进 contextvar,内层 agent
#      (含嵌套的 AgentTool / ChainAgent / 闭包里的 agent)在同一上下文里执行工具时都能看到它;
#      Coordinator.run_parallel 把调用方上下文复制进每个工作线程,故并行编排下闸同样生效。
#   2. 静态绑定(require_approval):把闸直接包进工具本身——不依赖任何上下文,裸线程 / 第三方 agent
#      执行它也绕不过去。
# 未配置任何审批时,执行点只做一次 contextvar 读取即返回(零行为变化)。


@dataclass(frozen=True)
class _ApprovalGuard:
    """一份生效中的审批配置:门 + 受审批工具名集 + 类别 code(+ 可选 trace 落点)。"""

    gate: ApprovalGate
    gated: frozenset[str]
    code: str = APPROVAL_TOOL_CALL
    trace: TraceSink | None = None
    agent: str = ""
    step: int = 0

    def check(self, tool: str, arguments: Mapping[str, object]) -> None:
        """对一次真实工具调用审批:approved 返回;rejected / pending / 门故障一律抛错(不执行)。"""
        if tool not in self.gated:
            return
        request = make_approval_request(self.code, tool, arguments, bind_values=True)
        try:
            decision = self.gate.review(request)
        except Exception as exc:  # noqa: BLE001 —— fail-closed:门的任何故障都等同「未批准」
            raise ApprovalGateError(
                "审批门故障,受审批工具不执行(fail-closed)",
                request_id=request.id,
                tool=tool,
                cause=type(exc).__name__,
            ) from exc
        if not isinstance(decision, Decision):
            raise ApprovalGateError(
                "审批门返回了非 Decision 结果,受审批工具不执行(fail-closed)",
                request_id=request.id,
                tool=tool,
            )
        if self.trace is not None:
            self.trace.emit(
                "mw_approval",
                agent=self.agent,
                step=self.step,
                gated_count=1,
                decision=decision.value,
            )
        if decision is Decision.APPROVED:
            return
        if decision is Decision.REJECTED:
            raise ApprovalRejected(
                "审批被拒:受审批工具调用被断路", request_id=request.id, tool=tool
            )
        raise ApprovalPending("审批待定:受审批工具调用待人类拍板", request_id=request.id, tool=tool)


_ACTIVE_GUARDS: ContextVar[tuple[_ApprovalGuard, ...]] = ContextVar(
    "spineagent_approval_guards", default=()
)


def enforce_tool_approval(tool: str, arguments: Mapping[str, object]) -> None:
    """工具执行点在【每一次】真实调用工具前调它:按当前生效的全部审批配置逐一审批。

    未配置审批时只读一次 contextvar 即返回(零行为变化);命中受审批工具时:approved 放行,
    rejected 抛 ApprovalRejected、pending 抛 ApprovalPending、门故障抛 ApprovalGateError——调用方
    据此【不执行】该工具。自定义 agent 若自己执行工具,也应在调用前调它(或改用 require_approval)。
    """
    for guard in _ACTIVE_GUARDS.get():
        guard.check(tool, arguments)


def _push_guard(guard: _ApprovalGuard) -> Callable[[], None]:
    """把一枚审批配置压进当前上下文,返回对应的弹出回调(须在同一上下文里调用)。"""
    token = _ACTIVE_GUARDS.set((*_ACTIVE_GUARDS.get(), guard))
    return lambda: _ACTIVE_GUARDS.reset(token)


class _ApprovalGatedTool:
    """require_approval 对单串参 Tool 的包装:run 前先过闸(参数按 {"arg": arg} 规范化)。"""

    def __init__(self, tool: Tool, guard: _ApprovalGuard) -> None:
        self._tool = tool
        self._guard = guard

    @property
    def name(self) -> str:
        return self._tool.name

    def run(self, arg: str) -> ToolResult:
        self._guard.check(self._tool.name, {"arg": arg})
        return self._tool.run(arg)


class _GuardedCall:
    """require_approval 对 FunctionTool 的包装函数:调用底层函数前先按真实 kwargs 过闸。"""

    def __init__(self, name: str, func: Callable[..., Any], guard: _ApprovalGuard) -> None:
        self._name = name
        self._func = func
        self._guard = guard

    def __call__(self, **kwargs: Any) -> Any:
        self._guard.check(self._name, kwargs)
        return self._func(**kwargs)


@overload
def require_approval(
    tool: FunctionTool, gate: ApprovalGate, *, code: str = APPROVAL_TOOL_CALL
) -> FunctionTool: ...
@overload
def require_approval(tool: Tool, gate: ApprovalGate, *, code: str = APPROVAL_TOOL_CALL) -> Tool: ...
def require_approval(
    tool: FunctionTool | Tool, gate: ApprovalGate, *, code: str = APPROVAL_TOOL_CALL
) -> FunctionTool | Tool:
    """把审批闸【绑进工具本身】:无论哪个 agent、哪条线程执行它,调用前都先过闸。

    与 ApprovalMiddleware(动态作用域)互补:这条通道不依赖 contextvar,裸线程 / 第三方 agent 也
    绕不过去。两者叠加时同一调用会被同一 gate 审两次,决议幂等,结果一致。
    """
    guard = _ApprovalGuard(gate=gate, gated=frozenset({tool.name}), code=code)
    if isinstance(tool, FunctionTool):
        return replace(tool, func=_GuardedCall(tool.name, tool.func, guard))
    return _ApprovalGatedTool(tool, guard)


# ---- 审批 middleware(插进现有 middleware 链)-------------------------------------------------


class ApprovalMiddleware:
    """审批门 middleware:把审批配置下沉到本步内【每一次真实工具调用】的执行点上。

    before_step 把 (gate, gated_tools) 压进当前上下文的审批作用域,MiddlewareAgent 在本步结束(含
    抛错)时弹出。作用域内,任何执行点(FunctionCallingAgent / ToolUsingAgent / 嵌套 agent /
    run_parallel 的工作线程)在调用受审批工具前都按「真实工具名 + 规范化参数」向 gate review:
    approved -> 执行;rejected -> 抛 ApprovalRejected(工具不执行);pending -> 抛 ApprovalPending 挂起
    run,等 out-of-band resolve 后重跑本步(同一工具 + 同一参数命中已落决议,放行);门故障 ->
    抛 ApprovalGateError(fail-closed)。编排层弹性模式经 corespine.error_to_dict 把它们归一进
    AgentResult.error。

    【为何不再检查 ctx.tools】ctx.tools 只是「声明的可用面」,与内层 agent 真正执行什么无关:不播种
    它、或重跑使步序变化,旧闸就看不见工具而放行(fail-open)。见 docs/adr/0002。

    【为何是「抛类型化错误」而非同步阻塞 / 新增续体机制】同 ADR 0001:pending 抛可重试、带 request_id
    的 ApprovalPending,调用方 resolve 后重跑 step 恢复;决议幂等使重跑安全。

    默认 gated_tools 为空 => before_step 恒早退,零行为变化(opt-in)。若在 MiddlewareAgent 之外手动
    调 before_step 却不跑 ctx.cleanups,作用域会留在当前上下文里——只会多拦、不会少拦(偏 fail-closed)。
    隐私:trace 只记 code / 计数 / 决议,绝不记工具参数正文。
    """

    def __init__(
        self,
        gate: ApprovalGate,
        *,
        gated_tools: Sequence[str] = (),
        code: str = APPROVAL_TOOL_CALL,
    ) -> None:
        self._gate = gate
        self._gated = frozenset(gated_tools)
        self._code = code

    def before_step(self, ctx: StepContext) -> None:
        if not self._gated:
            return  # 未配置受审批工具:零行为变化
        guard = _ApprovalGuard(
            gate=self._gate,
            gated=self._gated,
            code=self._code,
            trace=ctx.trace,
            agent=ctx.agent,
            step=ctx.step,
        )
        ctx.cleanups.append(_push_guard(guard))

    def after_step(self, ctx: StepContext, result: AgentResult) -> AgentResult:
        return result


# 把审批 middleware 登记进现有 middlewares 注册表(需显式传 gate=...;缺省 gated_tools 空即零行为变化)。
middlewares.register("approval", lambda **kw: ApprovalMiddleware(**kw))
