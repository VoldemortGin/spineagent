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

隐私:本缝实现【自身不发射 trace】(同 credential / trigger 缝);request 只带定位摘要,payload
正文到不了任何字段。要观测在【审批 middleware】里记 code / 计数 / 决议,绝不记参数正文。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol, runtime_checkable

from corespine.errors import CorespineError
from corespine.seam.registry import Registry

from spineagent.agent.agent import AgentResult
from spineagent.agent.middleware import StepContext, middlewares

# 内置审批类别 code(domain-neutral;调用方用它路由 / 计数)。
APPROVAL_TOOL_CALL = "tool_call"


class Decision(str, Enum):
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


def _fingerprint(names_or_args: Sequence[str] | Mapping[str, object]) -> str:
    """据参数 schema 形状(键名 + 值类型)或一组名字派生确定性指纹——绝不含任何值 / 正文。"""
    if isinstance(names_or_args, Mapping):
        shape: list[list[str]] = sorted(
            [str(k), type(v).__name__] for k, v in names_or_args.items()
        )
    else:
        shape = sorted([str(n), ""] for n in names_or_args)
    raw = json.dumps(shape, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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
) -> ApprovalRequest:
    """从工具名 + 参数字典构造一次待审批请求(只取 schema 指纹与计数,绝不落参数值)。

    nonce 让调用方在需要「按次审批」时强制区分 schema 相同的两次调用;缺省 "" 时 schema 相同的
    请求折叠成同一 id(决议可复用,幂等)。
    """
    args = dict(arguments or {})
    fp = _fingerprint(args)
    return ApprovalRequest(
        code=code,
        id=_request_id(code, tool, fp, nonce),
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


# ---- 审批 middleware(插进现有 middleware 链)-------------------------------------------------


def _request_for_step(code: str, gated_names: Sequence[str]) -> ApprovalRequest:
    """据本步激活的受审批工具名集构造一次待审批请求。

    request id 【刻意排除步序计数器】——只由 (code, 工具集, schema 指纹) 派生,故挂起的 run 在
    resolve 后重跑 step(步序会自增)仍稳定命中同一 request id 与已落决议,无需持久化 agent 循环
    续体。代价是:同一 (code, 工具集) 的决议会被记住并幂等复用;要按次审批就换 code / 换 gate。
    """
    tool = ",".join(gated_names)
    fp = _fingerprint(gated_names)
    return ApprovalRequest(
        code=code,
        id=_request_id(code, tool, fp, ""),
        tool=tool,
        arg_fingerprint=fp,
        arg_count=len(gated_names),
    )


class ApprovalMiddleware:
    """审批门 middleware:步执行前,若本步激活了受审批工具,则询问 ApprovalGate 再决定放行 / 断路。

    approved -> 放行(before_step 返回 None,内层 Agent 照常跑);rejected -> 抛 ApprovalRejected
    (断路:内层 Agent 不跑;编排层弹性模式经 corespine.error_to_dict 把它归一进 AgentResult.error,
    即「类型化错误返回给 agent 循环」——不 panic、不裸吞);pending -> 抛 ApprovalPending 挂起 run,
    等 out-of-band resolve 后凭 resume token 重跑本步(届时 gate 已落决议、review 返回 approved)。

    【为何是「抛类型化错误」而非同步阻塞 / 新增续体机制(裁决 + 理由)】同步阻塞会冻住线程等人,
    不可接受;而给 agent 循环加「挂起 / 恢复续体」是对 ToolUsingAgent / FunctionCallingAgent /
    MiddlewareAgent 的大改。家族既有的断路手段正是【抛类型化 CorespineError -> 编排层捕获进
    AgentResult.error】(见 AgentResult docstring 与 Coordinator resilient)。pending 复用这条缝:
    抛一个可重试的、带 request_id 的 ApprovalPending,由调用方 resolve 后重跑 step 恢复——step 可
    安全重跑正因 gate 的决议幂等(review 稳定)。这【零新增】agent 循环机制,纯组合既有模式。

    默认 gated_tools 为空 => before_step 恒早退,零行为变化(opt-in);哪些工具需审批由策略配置。
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
        gated_here = sorted(t for t in ctx.tools if t in self._gated)
        if not gated_here:
            return  # 本步未激活任何受审批工具:零行为变化
        request = _request_for_step(self._code, gated_here)
        decision = self._gate.review(request)
        if ctx.trace is not None:
            ctx.trace.emit(
                "mw_approval",
                agent=ctx.agent,
                step=ctx.step,
                gated_count=len(gated_here),
                decision=decision.value,
            )
        if decision is Decision.APPROVED:
            return
        if decision is Decision.REJECTED:
            raise ApprovalRejected(
                f"审批被拒:本步 {len(gated_here)} 个受审批工具被断路",
                request_id=request.id,
                tool=request.tool,
            )
        raise ApprovalPending(
            f"审批待定:本步 {len(gated_here)} 个受审批工具待人类拍板",
            request_id=request.id,
            tool=request.tool,
        )

    def after_step(self, ctx: StepContext, result: AgentResult) -> AgentResult:
        return result


# 把审批 middleware 登记进现有 middlewares 注册表(需显式传 gate=...;缺省 gated_tools 空即零行为变化)。
middlewares.register("approval", lambda **kw: ApprovalMiddleware(**kw))
