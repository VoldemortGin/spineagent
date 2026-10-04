"""approval 缝:审批门(ApprovalGate)—— agent 触及风险动作前暂停,等人类批准 / 拒绝后恢复。

对标 n8n Wait 节点与 Send-and-Wait 审批的【概念】(n8n 是 fair-code 许可,只学概念、绝不抄代码):
把家族从「一路跑到底的 agent」扩成「能在风险动作前停下等人拍板」的系统。本批只做框架机制;
产品侧的审批收件箱 UI 留给 spinestudio 后续批。

家族缝的元模式(同 trigger / credential / sandbox 缝):**Protocol + 离线确定性默认 + Registry
工厂 + 参数化 conformance**。一次「待审批请求」的定位摘要——审批类别 code、确定性 request id、
触发的工具名、参数 schema 指纹与计数、作用域——不含参数值;另带只给审批人看的完整规范化参数与
列表用的预览(不进 trace / repr)。gate 对它给出三态决议之一:approved / rejected / pending。

两个离线确定性默认:
  - AutoApprovalGate —— 策略表:按工具名 glob 模式 allow / deny,纯配置、确定性,永不 pending;
  - ManualApprovalGate —— 进程内挂起:review 登记待审、显式 resolve(request_id, decision) 落决议并
    铸一枚【一次性 resume token】(secrets 随机、只存 sha256 哈希、用过即废,模式照 spinestudio
    refresh_store,但这里是框架内存 / 可插拔 ResumeTokenStore)。

【批准语义裁决(钉死,见 docs/adr/0002 决策 4)】review 是纯查询(其间无 resolve / consume 时恒返回
同一决议);批准缺省【一次性消费】:执行闸在执行前经 consume 核销一次放行额度(resolve(uses=N) 放行
N 次,uses=None 为显式可选的幂等模式)。request 绑定作用域(会话 / 用户 / run),A 的批准对 B 无效。
resolve 只接受已登记、未过期的请求(不得预先批准),以【冲突】决议 resolve 抛 ApprovalConflict
(先落者胜,绝不静默翻转)。

【resume token 一次性裁决(钉死)】token 明文只在 resolve 那一刻返回一次,落库只存 sha256 哈希;
redeem 校验 + 标记消费,重放(再次 redeem 同一 token)必抛 InvalidResumeToken——泄露的 token 也
无从二次恢复。

【执行闸裁决(钉死,见 docs/adr/0002)】审批是【工具调用点上的强制闸】,不是对 ctx.tools 声明的
检查:执行点在每一次真实调用工具前调 enforce_tool_approval(真实工具名, 参数);request id 由
(code, 工具名, schema 指纹, 规范化参数值的 sha256, 作用域)派生,不依赖步序;门故障 fail-closed;
受审批工具名写错 / 含通配符时 fail-closed(ApprovalConfigError)。

隐私:本缝实现【自身不发射 trace】(同 credential / trigger 缝)。要观测在【审批 middleware】里记
code / 计数 / 决议,绝不记参数正文或预览;完整参数与预览只经审批接口(gate.review / pending())给审批人。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol, overload, runtime_checkable

from corespine.errors import CorespineError
from corespine.observability.trace import TraceSink
from corespine.seam.registry import Registry

from spineagent.agent.agent import AgentResult
from spineagent.agent.middleware import StepContext, middlewares
from spineagent.tools.function_tool import FunctionTool
from spineagent.tools.tool import Tool, ToolResult, reachable_tool_names

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
    """审批待定:run 挂起,等 out-of-band resolve 后在【同一作用域】里重跑。可重试(等人拍板)。

    request_id / scope 收进 context,供调用方 / 编排层据它 resolve(与审批收件箱对接)。

    【至少一次(at-least-once)】重跑是从头再跑这个 run:本 run 里此前已经执行过的工具(含未受审批的
    send_email 之类、以及此前已获批并执行过的受审批调用)会【再次执行】。库不记录、不复用任何跨 run 的
    工具结果。调用方应让工具幂等;或改用 FunctionCallingAgent(on_approval="feed_back") 的不中断模式
    (run 不中断,没有重跑,也就没有重放)。见 docs/adr/0002 决策 5a。
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


class UnknownApprovalRequest(ApprovalError):
    """resolve 了一个不存在 / 已过期 / 已被核销的请求(不得预先批准离线算出的 id)。"""

    code = "approval.unknown_request"


class ApprovalConfigError(ApprovalError, ValueError):
    """审批配置错误(受审批工具名含通配 / 对应不到已知工具 / 与已注册工具仅大小写或分隔符不同)。

    fail-closed:在任何工具执行之前抛出,绝不让写错的名字静默放行。
    """

    code = "approval.config_error"


_PENDING_MESSAGE = (
    "审批待定:受审批工具调用待人类拍板。批准后在同一作用域里重跑本 run 时,此前已执行过的工具会再次执行"
    "(至少一次语义;工具应幂等,或改用 on_approval='feed_back')"
)


# ---- 请求:定位摘要 + 给审批人看的完整参数 / 预览 ------------------------------------------------


@dataclass(frozen=True)
class ApprovalRequest:
    """一次待审批请求(只读):定位摘要(code / id / 工具名 / schema 指纹 / 计数 / 作用域)+ 给审批人的内容。

    - code:审批类别 code(如 "tool_call";调用方据它路由 / 计数);
    - id:确定性 request id(code + 工具名 + schema 指纹 + 完整规范化参数的 sha256 + 作用域派生):批准 X 只放行
      字节完全相同的 X;
    - tool:触发审批的工具名(定位符,非正文);
    - arg_fingerprint:参数的 schema 指纹(sha256 前 16 位,只覆盖【键名 + 值类型】,绝不含值);
    - arg_count:参数个数(计数);
    - scope:请求所属的作用域(调用方给的不透明标识,已折进 id;A 作用域的批准对 B 无效);
    - canonical_arguments:完整规范化参数(键排序紧凑 JSON,非 JSON 值退回 repr)——正是 id 所哈希的内容。
      审批人据它(`arguments()`)做决定;
    - preview:列表展示用的预览((键, 值文本) 元组;长值截断一次并如实注明省略的字符数与该值的摘要前缀;
      只对调用方显式声明的敏感参数打码)。审批人不应只凭 preview 做决定。

    canonical_arguments 与 preview 只经审批接口(gate.review / ManualApprovalGate.pending)给审批人:不进 repr、
    不进 trace / 日志(trace 只记 code / 计数 / 决议,见 docs/adr/0002)。
    """

    code: str
    id: str
    tool: str
    arg_fingerprint: str = ""
    arg_count: int = 0
    scope: str = field(default="", repr=False)
    canonical_arguments: str = field(default="", repr=False)
    preview: tuple[tuple[str, str], ...] = field(default=(), repr=False, compare=False)

    def arguments(self) -> dict[str, Any]:
        """完整规范化参数(每次返回新 dict;未打码——审批人据它决定放不放行)。"""
        if not self.canonical_arguments:
            return {}
        loaded: dict[str, Any] = json.loads(self.canonical_arguments)
        return loaded


def _fingerprint(args: Mapping[str, object]) -> str:
    """据参数 schema 形状(键名 + 值类型)派生确定性指纹——绝不含任何值 / 正文。"""
    shape = sorted([str(k), type(v).__name__] for k, v in args.items())
    raw = json.dumps(shape, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _canonical(value: object) -> str:
    """规范化:键排序紧凑 JSON;非 JSON 值退回 repr(它若不稳定,只会让请求对不上旧批准而重新审批)。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=repr
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _request_id(code: str, tool: str, fingerprint: str, nonce: str, scope: str = "") -> str:
    """据 code + 工具名 + schema 指纹 + nonce(+ 作用域)派生确定性 request id(纯函数,可重放)。"""
    parts = [code, tool, fingerprint, nonce] + ([f"scope={scope}"] if scope else [])
    return _sha256(":".join(parts))[:16]


# 预览里单个值 / 键的最大字符数:超出只截断一次,并注明省略的字符数与该值的摘要前缀(尾部不同的两个值可区分)。
_PREVIEW_MAX_CHARS = 200
_PREVIEW_MAX_KEY_CHARS = 64


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(省略 {len(text) - limit} 字符,sha256:{_sha256(text)[:12]})"


def _masked(value: object) -> str:
    """被声明为敏感的值:打码,附完整值的摘要前缀(不同的值仍可区分)。"""
    return f"***(sha256:{_sha256(_canonical(value))[:12]})"


def _mask_paths(value: object, paths: Collection[tuple[str, ...]]) -> object:
    """按参数路径(只穿过 dict)把声明为敏感的嵌套值换成打码文本;返回副本,绝不改原值。"""
    if not paths or not isinstance(value, Mapping):
        return value
    out: dict[str, object] = {}
    for key, item in value.items():
        here = str(key)
        if (here,) in paths:
            out[here] = _masked(item)
        else:
            deeper = [path[1:] for path in paths if len(path) > 1 and path[0] == here]
            out[here] = _mask_paths(item, deeper)
    return out


def _build_preview(
    args: Mapping[str, object], sensitive: Collection[str]
) -> tuple[tuple[str, str], ...]:
    """按键排序生成预览:缺省不打码;只对 sensitive 里声明的参数路径("password" / "body.to_token")打码。"""
    paths = [tuple(path.split(".")) for path in sensitive]
    preview: list[tuple[str, str]] = []
    for key in sorted(str(k) for k in args):
        value = args[key]
        if (key,) in paths:
            text = _masked(value)
        elif isinstance(value, str):
            text = repr(value)
        else:
            nested = [path[1:] for path in paths if len(path) > 1 and path[0] == key]
            text = json.dumps(
                _mask_paths(value, nested), ensure_ascii=False, sort_keys=True, default=repr
            )
        preview.append((_clip(key, _PREVIEW_MAX_KEY_CHARS), _clip(text, _PREVIEW_MAX_CHARS)))
    return tuple(preview)


def make_approval_request(
    code: str,
    tool: str,
    arguments: Mapping[str, object] | None = None,
    *,
    nonce: str = "",
    bind_values: bool = False,
    scope: str = "",
    sensitive_args: Collection[str] = (),
) -> ApprovalRequest:
    """从工具名 + 参数字典构造一次待审批请求(定位摘要只取 schema 指纹与计数;完整参数只给审批人)。

    nonce 让调用方在需要「按次审批」时强制区分 schema 相同的两次调用;缺省 "" 时 schema 相同的
    请求折叠成同一 id。bind_values=True 时把【完整规范化参数的 sha256】也折进 request id:参数一变即是
    新请求、须重新审批——工具调用点上的执行闸用的就是它。scope 把请求绑定到调用方的作用域(折进 id)。
    sensitive_args 声明预览里要打码的参数路径(点号分隔,只穿过 dict);完整参数不受影响。
    """
    args = {str(k): v for k, v in (arguments or {}).items()}
    canonical = _canonical(args)
    fp = _fingerprint(args)
    salt = nonce if not bind_values else f"{nonce}:{_sha256(canonical)}"
    return ApprovalRequest(
        code=code,
        id=_request_id(code, tool, fp, salt, scope),
        tool=tool,
        arg_fingerprint=fp,
        arg_count=len(args),
        scope=scope,
        canonical_arguments=canonical,
        preview=_build_preview(args, sensitive_args),
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


@runtime_checkable
class ConsumableApprovalGate(ApprovalGate, Protocol):
    """可核销的审批门:批准是【有限次数】的放行额度,执行闸在真正执行前调 consume 核销一次。

    review 仍是纯查询(幂等);consume(request) 原子地扣减一次放行额度,扣到了返回 True,额度已用尽 /
    不存在 / 已过期返回 False(执行闸据此判「未批准」、不执行)。不实现 consume 的门(如策略表
    AutoApprovalGate)的批准是常驻的策略放行。
    """

    def consume(self, request: ApprovalRequest) -> bool: ...


# ---- 一次性 resume token 存储(可插拔;框架内存默认)------------------------------------------


@dataclass(frozen=True)
class ResumeTicket:
    """一次成功 redeem 的凭据:指向被授权恢复的 request、其决议与作用域(resume 流程的句柄)。

    ticket 【不是】执行凭据:它只告诉调用方「哪个请求在哪个作用域里被批准了」,调用方据此在同一
    作用域里重跑(`with approval_scope(ticket.scope): agent.step(...)`)。放行额度在执行点核销,
    故持有 / 重放 ticket 都不能让同一批准多执行一次。
    """

    request_id: str
    decision: Decision
    scope: str = ""


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


# ManualApprovalGate 缺省:请求表上限与请求存活期(秒)。
_DEFAULT_MAX_REQUESTS = 1_024
_DEFAULT_REQUEST_TTL = 3_600.0


@dataclass
class _RequestRecord:
    request: ApprovalRequest
    created: float
    expires: float
    decision: Decision = Decision.PENDING
    uses_left: int | None = None  # 仅 APPROVED 有意义:None = 不限次(显式可选的幂等模式)


class ManualApprovalGate:
    """进程内挂起审批门:review 登记待审,resolve 落决议 + 铸一次性 resume token,执行时核销批准。

    - review(request):首见即登记为待审(供 pending() 列举,对接审批收件箱);返回当前决议。纯查询,
      不消耗批准额度。
    - resolve(request_id, decision, *, uses=1, ttl_seconds=None):只能决议【已登记、未过期】的请求
      (未知 id 抛 UnknownApprovalRequest——不得预先批准离线算出的 id)。批准缺省只放行【一次】
      匹配的工具调用(uses=1,执行时核销);uses=N 放行 N 次;uses=None 是旧的「决议幂等」模式
      (有效期内同一请求可无限次执行——风险:一次批准可被同一作用域里任意多次的同参调用复用)。
    - consume(request):执行闸在执行前调用,原子核销一次;额度用尽即删除记录,同一请求再来会重新
      登记为待审。
    - pending():列举待审请求,带完整规范化参数(arguments())与预览(只给审批人看,绝不进 trace)。

    请求表有上限(max_requests,满了先清过期、再淘汰最早登记的)与存活期(request_ttl 秒;批准 /
    拒绝的有效期缺省同此,可由 resolve 的 ttl_seconds 单独指定),防无界增长。时钟可注入。线程安全。
    冲突决议抛 ApprovalConflict(先落者胜,绝不静默翻转)。
    """

    name = "manual"

    def __init__(
        self,
        *,
        token_store: ResumeTokenStore | None = None,
        max_requests: int = _DEFAULT_MAX_REQUESTS,
        request_ttl: float = _DEFAULT_REQUEST_TTL,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests < 1:
            raise ValueError(f"max_requests 必须 ≥ 1:{max_requests}")
        if request_ttl <= 0:
            raise ValueError(f"request_ttl 必须为正:{request_ttl}")
        self._records: dict[str, _RequestRecord] = {}
        self._ticket_scopes: dict[str, str] = {}
        self._store: ResumeTokenStore = token_store or InMemoryResumeTokenStore()
        self._max_requests = max_requests
        self._request_ttl = request_ttl
        self._now = now_fn
        self._lock = threading.Lock()

    def _live(self, request_id: str, now: float) -> _RequestRecord | None:
        record = self._records.get(request_id)
        if record is not None and now >= record.expires:
            del self._records[request_id]
            return None
        return record

    def _make_room(self, now: float) -> None:
        if len(self._records) < self._max_requests:
            return
        for rid in [rid for rid, rec in self._records.items() if now >= rec.expires]:
            del self._records[rid]
        while len(self._records) >= self._max_requests:
            oldest = min(self._records, key=lambda rid: self._records[rid].created)
            del self._records[oldest]

    def review(self, request: ApprovalRequest) -> Decision:
        with self._lock:
            now = self._now()
            record = self._live(request.id, now)
            if record is None:
                self._make_room(now)
                self._records[request.id] = _RequestRecord(
                    request=request, created=now, expires=now + self._request_ttl
                )
                return Decision.PENDING
            return record.decision

    def consume(self, request: ApprovalRequest) -> bool:
        with self._lock:
            record = self._live(request.id, self._now())
            # 批准绑定到登记时的请求对象:id 相同还不够,必须是同一条登记记录的同一请求(code / 工具 /
            # 参数指纹与计数 / 作用域逐字段相等)。
            if (
                record is None
                or record.decision is not Decision.APPROVED
                or record.request != request
            ):
                return False
            if record.uses_left is not None:
                record.uses_left -= 1
                if record.uses_left <= 0:
                    del self._records[request.id]
            return True

    def resolve(
        self,
        request_id: str,
        decision: Decision,
        *,
        uses: int | None = 1,
        ttl_seconds: float | None = None,
    ) -> str:
        """对一个【已登记、未过期】的待审请求落 approved / rejected 决议,并铸一枚一次性 resume token。"""
        if decision not in (Decision.APPROVED, Decision.REJECTED):
            raise ValueError(f"resolve 只接受 approved / rejected,收到:{decision!r}")
        if uses is not None and uses < 1:
            raise ValueError(f"uses 必须 ≥ 1 或为 None(不限次):{uses}")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds 必须为正:{ttl_seconds}")
        with self._lock:
            now = self._now()
            record = self._live(request_id, now)
            if record is None:
                raise UnknownApprovalRequest(
                    "只能决议已登记、未过期的待审请求", request_id=request_id
                )
            if record.decision is Decision.PENDING:
                record.decision = decision
                record.uses_left = uses if decision is Decision.APPROVED else None
                record.expires = now + (
                    ttl_seconds if ttl_seconds is not None else self._request_ttl
                )
            elif record.decision is not decision:
                raise ApprovalConflict(
                    f"request {request_id!r} 已落决议 {record.decision.value!r},"
                    f"不接受冲突决议 {decision.value!r}",
                    request_id=request_id,
                )
            # 相同决议重复 resolve 幂等:不追加额度,只另铸一枚独立的一次性 token。
            self._ticket_scopes[request_id] = record.request.scope
            while len(self._ticket_scopes) > self._max_requests:
                del self._ticket_scopes[next(iter(self._ticket_scopes))]
        return self._store.issue(request_id, decision)

    def redeem(self, raw_token: str) -> ResumeTicket:
        """消费一枚一次性 resume token(重放必败),拿回被授权恢复的 request、决议与作用域。"""
        ticket = self._store.redeem(raw_token)
        with self._lock:
            scope = self._ticket_scopes.get(ticket.request_id, ticket.scope)
        return replace(ticket, scope=scope)

    def pending(self) -> list[ApprovalRequest]:
        """列举未过期、尚未落决议的待审请求(按 id 字典序;带给审批人看的完整参数与预览)。"""
        with self._lock:
            now = self._now()
            return [
                rec.request
                for rid, rec in sorted(self._records.items())
                if rec.decision is Decision.PENDING and now < rec.expires
            ]

    def __repr__(self) -> str:
        with self._lock:
            undecided = sum(1 for rec in self._records.values() if rec.decision is Decision.PENDING)
            return f"ManualApprovalGate(seen={len(self._records)}, pending={undecided})"


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


_GLOB_CHARS = frozenset("*?[]")


def _validate_gated_names(names: Sequence[str]) -> frozenset[str]:
    """受审批工具名必须是确切名字:空名 / 非 str / 含通配符一律拒绝(不支持 glob,绝不静默放行)。"""
    validated: set[str] = set()
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise ApprovalConfigError("受审批工具名必须是非空字符串")
        if _GLOB_CHARS & set(name):
            raise ApprovalConfigError(
                "受审批工具名不支持通配符;请列出确切工具名,或用 require_approval 按工具对象绑定",
                name=name,
            )
        validated.add(name)
    return frozenset(validated)


def _normalized(name: str) -> str:
    return re.sub(r"[-_\s]", "", name).casefold()


def _check_near_miss(gated: frozenset[str], available: Collection[str]) -> None:
    """受审批名与本执行点已注册工具仅大小写 / 分隔符不同 -> 多半是写错:fail-closed,任何工具都不执行。"""
    for name in gated:
        if name in available:
            continue
        target = _normalized(name)
        for registered in available:
            if registered != name and _normalized(registered) == target:
                raise ApprovalConfigError(
                    "受审批工具名与已注册工具仅大小写 / 分隔符不同(多半是写错),fail-closed",
                    name=name,
                    registered=registered,
                )


_APPROVAL_SCOPE: ContextVar[str | None] = ContextVar("spineagent_approval_scope", default=None)


@contextmanager
def approval_scope(scope: str) -> Iterator[str]:
    """在 with 块内把审批作用域设为 scope(会话 / 用户 / run 的不透明标识)。

    作用域折进 request id:A 作用域的批准对 B 无效。它是调用方给的不透明字符串,库无法保证唯一——
    必须在共享同一个门的所有调用方之间唯一(建议 租户 id + 会话 id)。会产生待审请求的门要求显式作用域
    (这里设的外层作用域、或 ApprovalMiddleware / require_approval 的 scope=),库不生成隐式作用域。
    resume:批准后在同一作用域里重跑。只作用于当前上下文;自建线程需自己带过去
    (spineagent.orchestration.coordinator.bind_context)。
    """
    if not isinstance(scope, str) or not scope:
        raise ValueError("approval scope 必须是非空字符串")
    token = _APPROVAL_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _APPROVAL_SCOPE.reset(token)


def current_approval_scope() -> str | None:
    """当前上下文里生效的审批作用域(未设为 None)。"""
    return _APPROVAL_SCOPE.get()


# 会产生待审请求的门没有显式作用域时的配置错误(信息原文写进文档,调用方据它定位)。
_SCOPE_REQUIRED = (
    "这个审批门会产生待审请求(需要人工决议 / 可核销),必须显式提供作用域:请传入 "
    "scope=<会话或用户的唯一标识>(ApprovalMiddleware(scope=...) / require_approval(scope=...),"
    "或在外层 with approval_scope(...))。原因:作用域折进 request id、决定批准归谁;库不再隐式生成作用域"
    "——隐式作用域要么让批准后的重跑永远对不上原请求,要么让共享同一个门的调用方共用批准。"
    "作用域须在共享同一个门的所有调用方之间唯一(建议 租户 id + 会话 id)。"
)


def _requires_scope(gate: ApprovalGate) -> bool:
    """可核销的门(如 ManualApprovalGate)会产生需要人工决议的待审请求:必须有显式作用域。"""
    return isinstance(gate, ConsumableApprovalGate)


@dataclass(frozen=True)
class _ApprovalGuard:
    """一份生效中的审批配置:门 + 受审批工具名集 + 类别 code + 作用域 + 预览打码声明(+ 可选 trace 落点)。"""

    gate: ApprovalGate
    gated: frozenset[str]
    code: str = APPROVAL_TOOL_CALL
    trace: TraceSink | None = None
    agent: str = ""
    step: int = 0
    scope: str | None = None  # None:执行时取外层 approval_scope(都没有则见 resolve_scope)
    # 工具名 -> 预览里要打码的参数路径(调用方显式声明;缺省不打码)。
    sensitive: Mapping[str, frozenset[str]] = field(default_factory=dict, compare=False)

    def resolve_scope(self) -> str:
        """显式作用域 > 外层 approval_scope;都没有时:会产生待审请求的门报配置错误,同步门用空作用域。"""
        scope = self.scope if self.scope is not None else _APPROVAL_SCOPE.get()
        if scope is not None:
            return scope
        if _requires_scope(self.gate):
            raise ApprovalConfigError(_SCOPE_REQUIRED)
        return ""

    def check(
        self,
        tool: str,
        arguments: Mapping[str, object],
        *,
        available: Collection[str] = (),
        seen: set[tuple[int, str]] | None = None,
    ) -> None:
        """对一次真实工具调用审批:approved(且核销到额度)返回;否则一律抛错(不执行)。"""
        self._decide(tool, arguments, available=available, seen=seen, consume=True)

    def peek(
        self, tool: str, arguments: Mapping[str, object], *, available: Collection[str] = ()
    ) -> None:
        """只 review、不核销:未放行时抛与 check 相同的错误(供预检)。"""
        self._decide(tool, arguments, available=available, seen=None, consume=False)

    def _decide(
        self,
        tool: str,
        arguments: Mapping[str, object],
        *,
        available: Collection[str],
        seen: set[tuple[int, str]] | None,
        consume: bool,
    ) -> None:
        if available:
            _check_near_miss(self.gated, available)
        if tool not in self.gated:
            return
        request = make_approval_request(
            self.code,
            tool,
            arguments,
            bind_values=True,
            scope=self.resolve_scope(),
            sensitive_args=self.sensitive.get(tool, frozenset()),
        )
        key = (id(self.gate), request.id)
        if seen is not None and key in seen:
            return  # 同一次调用已被同一个门批准并核销(叠加的审批配置不重复消耗额度)
        decision = self._review(request, tool)
        if (
            consume
            and decision is Decision.APPROVED
            and isinstance(self.gate, ConsumableApprovalGate)
            and not self._consume(request, tool)
        ):
            # 额度已被用尽(一次性批准已被核销):重新登记为待审,本次不执行。
            decision = self._review(request, tool)
            if decision is Decision.APPROVED:
                decision = Decision.PENDING
        if decision is Decision.PENDING and not request.scope:
            # 不实现 consume 的第三方门也可能挂起:没有作用域的待审请求无法被正确恢复 / 归属。
            raise ApprovalConfigError(_SCOPE_REQUIRED)
        self._emit(decision)
        if decision is Decision.APPROVED:
            if seen is not None:
                seen.add(key)
            return
        if decision is Decision.REJECTED:
            raise ApprovalRejected(
                "审批被拒:受审批工具调用被断路",
                request_id=request.id,
                tool=tool,
                scope=request.scope,
            )
        raise ApprovalPending(
            _PENDING_MESSAGE,
            request_id=request.id,
            tool=tool,
            scope=request.scope,
        )

    def _emit(self, decision: Decision) -> None:
        # 隐私:只记 code / 计数 / 决议,绝不记参数正文或预览。
        if self.trace is not None:
            self.trace.emit(
                "mw_approval",
                agent=self.agent,
                step=self.step,
                gated_count=1,
                decision=decision.value,
            )

    def _review(self, request: ApprovalRequest, tool: str) -> Decision:
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
        return decision

    def _consume(self, request: ApprovalRequest, tool: str) -> bool:
        gate: Any = self.gate
        try:
            consumed = gate.consume(request)
        except Exception as exc:  # noqa: BLE001 —— fail-closed
            raise ApprovalGateError(
                "审批门核销故障,受审批工具不执行(fail-closed)",
                request_id=request.id,
                tool=tool,
                cause=type(exc).__name__,
            ) from exc
        return consumed is True


_ACTIVE_GUARDS: ContextVar[tuple[_ApprovalGuard, ...]] = ContextVar(
    "spineagent_approval_guards", default=()
)


def enforce_tool_approval(
    tool: str,
    arguments: Mapping[str, object],
    *,
    target: object | None = None,
    available: Collection[str] = (),
) -> None:
    """工具执行点在【每一次】真实调用工具前调它:按当前生效的全部审批配置逐一审批并核销。

    未配置审批时只读一次 contextvar 即返回(零行为变化);命中受审批工具时:approved 放行(可核销的
    门在此扣减一次额度),rejected 抛 ApprovalRejected、pending 抛 ApprovalPending、门故障抛
    ApprovalGateError——调用方据此【不执行】该工具。target 传被执行的工具对象:若它自带
    require_approval 闸,同一个门交给工具自己审(避免同一调用被同一个门审两次、扣两次额度)。
    available 传本执行点已注册的工具名:受审批名与之仅大小写 / 分隔符不同时 fail-closed。
    自定义 agent 若自己执行工具,也应在调用前调它(或改用 require_approval)。
    """
    guards = _ACTIVE_GUARDS.get()
    if not guards:
        return
    skip = _wrapper_gate_ids(target)
    seen: set[tuple[int, str]] = set()
    for guard in guards:
        if id(guard.gate) in skip:
            if available:
                _check_near_miss(guard.gated, available)
            continue
        guard.check(tool, arguments, available=available, seen=seen)


def preflight_tool_approvals(
    calls: Sequence[tuple[str, Mapping[str, object], object | None]],
    *,
    available: Collection[str] = (),
) -> list[ApprovalError | None]:
    """预检:在执行任何一个之前,查询这批待执行的工具调用里哪些现在不会被放行(不核销任何额度)。

    calls 是 (工具名, 参数, 工具对象或 None)。对每个受审批调用向门 review(待审的会被登记,审批人
    能一次看到整批);返回与 calls 等长的列表:可放行为 None,否则是对应的错误(ApprovalPending /
    ApprovalRejected / ApprovalGateError / ApprovalConfigError)。供「先审后行」使用:调用方据此
    选择整批先审批、再执行。注意预检不核销:同一批里同参调用多于剩余额度时,执行时仍可能挂起。
    """
    errors: list[ApprovalError | None] = []
    guards = _ACTIVE_GUARDS.get()
    for tool, arguments, target in calls:
        wrappers = _wrapper_guards(target)
        skip = {id(guard.gate) for guard in wrappers}
        try:
            for guard in wrappers:
                guard.peek(tool, arguments)
            for guard in guards:
                if id(guard.gate) in skip:
                    if available:
                        _check_near_miss(guard.gated, available)
                    continue
                guard.peek(tool, arguments, available=available)
        except ApprovalError as exc:
            errors.append(exc)
        else:
            errors.append(None)
    return errors


def _push_guard(guard: _ApprovalGuard) -> Callable[[], None]:
    """把一枚审批配置压进当前上下文,返回对应的弹出回调(须在同一上下文里调用)。"""
    token = _ACTIVE_GUARDS.set((*_ACTIVE_GUARDS.get(), guard))
    return lambda: _ACTIVE_GUARDS.reset(token)


def _set_scope(scope: str) -> Callable[[], None]:
    """把审批作用域设进当前上下文,返回对应的复位回调。"""
    token = _APPROVAL_SCOPE.set(scope)
    return lambda: _APPROVAL_SCOPE.reset(token)


class _ApprovalGatedTool:
    """require_approval 对单串参 Tool 的包装:run 前先过闸(参数按 {"arg": arg} 规范化)。"""

    def __init__(self, tool: Tool, guard: _ApprovalGuard) -> None:
        self.inner = tool
        self.guard = guard

    @property
    def name(self) -> str:
        return self.inner.name

    def run(self, arg: str) -> ToolResult:
        self.guard.check(self.inner.name, {"arg": arg})
        return self.inner.run(arg)

    def tool_inventory(self) -> frozenset[str] | None:
        nested = reachable_tool_names(self.inner)
        return frozenset({self.inner.name}) | (nested or frozenset())


class _GuardedCall:
    """require_approval 对 FunctionTool 的包装函数:调用底层函数前先按真实 kwargs 过闸。"""

    def __init__(self, name: str, func: Callable[..., Any], guard: _ApprovalGuard) -> None:
        self._name = name
        self.inner = func
        self.guard = guard

    def __call__(self, **kwargs: Any) -> Any:
        self.guard.check(self._name, kwargs)
        return self.inner(**kwargs)


def _wrapper_guards(target: object | None) -> list[_ApprovalGuard]:
    """被执行工具自带的 require_approval 闸(可多层包装)。"""
    guards: list[_ApprovalGuard] = []
    node: object | None = target.func if isinstance(target, FunctionTool) else target
    while isinstance(node, (_GuardedCall, _ApprovalGatedTool)):
        guards.append(node.guard)
        node = node.inner
    return guards


def _wrapper_gate_ids(target: object | None) -> frozenset[int]:
    """被执行工具自带的 require_approval 闸所用的门(按对象身份)。"""
    return frozenset(id(guard.gate) for guard in _wrapper_guards(target))


@overload
def require_approval(
    tool: FunctionTool,
    gate: ApprovalGate,
    *,
    code: str = APPROVAL_TOOL_CALL,
    scope: str | None = None,
    sensitive_args: Collection[str] = (),
) -> FunctionTool: ...
@overload
def require_approval(
    tool: Tool,
    gate: ApprovalGate,
    *,
    code: str = APPROVAL_TOOL_CALL,
    scope: str | None = None,
    sensitive_args: Collection[str] = (),
) -> Tool: ...
def require_approval(
    tool: FunctionTool | Tool,
    gate: ApprovalGate,
    *,
    code: str = APPROVAL_TOOL_CALL,
    scope: str | None = None,
    sensitive_args: Collection[str] = (),
) -> FunctionTool | Tool:
    """把审批闸【绑进工具对象本身】:无论哪个 agent、哪条线程、以什么名字执行它,调用前都先过闸。

    【安全场景的首选】按名字 gate(ApprovalMiddleware 的 gated_tools)只认工具名:同一个函数换个名字
    再注册一遍、或调用方自建线程脱离动态作用域时都不设防;require_approval 绑在对象上,两者都挡得住。
    scope:显式作用域;不给时取【执行时】外层的 approval_scope(模块级共享的工具对象用这个,每个请求在
    自己的作用域里调用)。两者都没有、而门会产生待审请求时,调用时抛 ApprovalConfigError(不执行,也不按
    包装实例生成隐式作用域)。与 ApprovalMiddleware 用同一个门叠加时,同一调用只审 / 核销一次。
    """
    if scope is not None and (not isinstance(scope, str) or not scope):
        raise ValueError("approval scope 必须是非空字符串")
    guard = _ApprovalGuard(
        gate=gate,
        gated=frozenset({tool.name}),
        code=code,
        scope=scope,
        sensitive={tool.name: frozenset(sensitive_args)},
    )
    if isinstance(tool, FunctionTool):
        return replace(tool, func=_GuardedCall(tool.name, tool.func, guard))
    return _ApprovalGatedTool(tool, guard)


# ---- 审批 middleware(插进现有 middleware 链)-------------------------------------------------


class ApprovalMiddleware:
    """审批门 middleware:把审批配置下沉到本步内【每一次真实工具调用】的执行点上。

    before_step 把 (gate, gated_tools, 作用域) 压进当前上下文的审批作用域,MiddlewareAgent 在本步结束
    (含抛错)时弹出。作用域内,任何执行点(FunctionCallingAgent / ToolUsingAgent / 嵌套 agent /
    run_parallel 的工作线程)在调用受审批工具前都按「真实工具名 + 规范化参数 + 作用域」向 gate review:
    approved -> 核销一次额度后执行;rejected -> 抛 ApprovalRejected;pending -> 抛 ApprovalPending 挂起
    run(context 带 request_id / scope);门故障 -> 抛 ApprovalGateError(fail-closed)。

    作用域:构造参数 scope > 外层 approval_scope(...),并作为外层作用域传给嵌套 agent / require_approval
    包装。门会产生待审请求(可核销,如 ManualApprovalGate)而两者都没有时,before_step 在内层 agent 运行
    之前抛 ApprovalConfigError;同步门(AutoApprovalGate 等,立即给出决定)不需要作用域。resume:resolve
    后在【同一作用域】里重跑本步(至少一次语义,见 ApprovalPending)。

    受审批工具名在构造时校验(不支持通配符);首次使用时若能推断被包裹 agent 的工具清单(见
    spineagent.tools.tool.reachable_tool_names),对应不到已知工具的名字直接抛 ApprovalConfigError,
    推断不了(不透明 agent)时在执行点检测「仅大小写 / 分隔符不同」的写错。按名字 gate 对「同一函数
    以别名注册」不设防——安全场景首选 require_approval(按工具对象绑定)。

    审批内容:gate 收到的 ApprovalRequest 带完整规范化参数(arguments())与列表用的预览;sensitive_args
    (工具名 -> 参数路径)显式声明预览里要打码的字段,缺省不打码。两者只给
    审批人看;trace 只记 code / 计数 / 决议,绝不记参数正文或预览。默认 gated_tools 为空 =>
    before_step 恒早退,零行为变化(opt-in)。
    """

    def __init__(
        self,
        gate: ApprovalGate,
        *,
        gated_tools: Sequence[str] = (),
        code: str = APPROVAL_TOOL_CALL,
        scope: str | None = None,
        sensitive_args: Mapping[str, Collection[str]] | None = None,
    ) -> None:
        if scope is not None and (not isinstance(scope, str) or not scope):
            raise ValueError("approval scope 必须是非空字符串")
        self._gate = gate
        self._gated = _validate_gated_names(gated_tools)
        self._code = code
        self._scope = scope
        self._sensitive = {tool: frozenset(paths) for tool, paths in (sensitive_args or {}).items()}

    def before_step(self, ctx: StepContext) -> None:
        if not self._gated:
            return  # 未配置受审批工具:零行为变化
        inventory = reachable_tool_names(ctx.inner_agent)
        if inventory is not None:
            unknown = sorted(self._gated - inventory)
            if unknown:
                raise ApprovalConfigError(
                    "受审批工具名对应不到被包裹 agent 的任何已知工具(拼写 / 大小写?),fail-closed",
                    unknown=unknown,
                )
        scope = self._scope or _APPROVAL_SCOPE.get()
        if scope is None:
            if _requires_scope(self._gate):
                # 在内层 agent 运行之前就报错:不生成隐式作用域。
                raise ApprovalConfigError(_SCOPE_REQUIRED)
            scope = ""  # 同步门(立即给出决定、永不挂起)不需要作用域
        else:
            # 作用域同时设为外层作用域:嵌套的审批配置 / require_approval 包装与本步共享同一作用域。
            ctx.cleanups.append(_set_scope(scope))
        guard = _ApprovalGuard(
            gate=self._gate,
            gated=self._gated,
            code=self._code,
            trace=ctx.trace,
            agent=ctx.agent,
            step=ctx.step,
            scope=scope,
            sensitive=self._sensitive,
        )
        ctx.cleanups.append(_push_guard(guard))

    def after_step(self, ctx: StepContext, result: AgentResult) -> AgentResult:
        return result


# 把审批 middleware 登记进现有 middlewares 注册表(需显式传 gate=...;缺省 gated_tools 空即零行为变化)。
middlewares.register("approval", lambda **kw: ApprovalMiddleware(**kw))
