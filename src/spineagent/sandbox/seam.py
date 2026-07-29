"""sandbox 缝:Sandbox 协议 + 离线确定性默认(隔离执行 skill / tool 代码)。

家族缝的元模式(同 mcp / a2a / tool-policy 缝):Protocol + 离线确定性默认 + Registry 工厂 +
真实后端经可选 extra 延迟 import。一个 Sandbox 把「跑一段代码 / 命令」隔离起来:给定代码、
超时与资源上限,拿回一个【带 provenance(哪条 sandbox 产出)+ 资源记账】的结果。

【离线默认为何是「受限白名单求值器」而非「真容器」(诚实性取舍)】真正的隔离(无网络出口、
无文件系统逃逸、可抢占的 CPU/内存上限)只有 OS 级手段(子进程 + namespaces / seccomp、容器)
才给得起,且不跨平台、需重依赖。所以离线默认【不假装】是硬隔离沙箱:InProcessSandbox 是这条
缝的【确定性参照实现】——把代码限制成一个【纯表达式白名单小语言】(CalcTool safe-eval 思路的
扩展),用 AST 白名单【构造即保证】无网络出口、无文件系统逃逸(拒绝 Import / Attribute / 任意
Call / 非白名单 Name),用「AST 节点预算」作为确定性的 CPU 代理上限,用输出上限截断产出。真实
硬隔离后端(subprocess / container)走 [sandbox] extra 延迟 import,是使用者按其平台接入的事。

隐私:Sandbox 不发 trace(它是被 skill / agent 调用的底层执行原语);其结果只含代码自己的产出
与资源计数,由上层(skill.invoke / agent.step)决定如何记进隐私安全 trace。
"""

import ast
import math
import operator
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from corespine.errors import SeamError
from corespine.seam.registry import Registry, lazy_extra_import

# 容器后端要延迟 import 的第三方 SDK(装了 spineagent[sandbox] 才有);默认离线路径绝不 import。
_CONTAINER_SDK_MODULE = "docker"


@dataclass(frozen=True)
class ResourceUsage:
    """一次沙箱执行的资源记账:确定性的 ops(求值的 AST 节点数,CPU 代理)+ 输出字符数 + 墙钟秒。

    ops / output_chars 对同一输入恒定(可断言、可复现);wall_seconds 是【信息性】的耗时观测,
    仅供排障,不参与任何不变量断言(它随机器负载浮动)。
    """

    ops: int = 0
    output_chars: int = 0
    wall_seconds: float = 0.0


@dataclass(frozen=True)
class Limits:
    """一次沙箱执行的资源上限。None 表示该维度不限(退回后端默认)。

    - timeout_seconds:墙钟超时。InProcessSandbox 无法抢占同步求值,故仅【尽力/记录】(见模块
      docstring),真正的抢占式超时是 subprocess / container 后端的职责;
    - max_output_chars:产出字符上限,超出即判失败(任何后端都能事后度量,故这是最中立的上限);
    - max_ops:InProcessSandbox 求值的 AST 节点数上限(确定性 CPU 代理),超出即判失败。
    """

    timeout_seconds: float | None = None
    max_output_chars: int | None = None
    max_ops: int | None = None


# 缺省上限:离线默认路径开箱即用的温和护栏(可被 run() 的 limits / timeout 覆盖)。
DEFAULT_LIMITS = Limits(timeout_seconds=5.0, max_output_chars=64_000, max_ops=100_000)

# InProcessSandbox 与宿主进程共享内存，节点数预算本身挡不住少量 AST 节点制造巨值
# （如 ``"x" * 10**9`` / ``2 ** 10**9``）。这些硬上限先于真实运算检查，属于
# 进程内参照实现不可关闭的安全边界；需要更大计算面的调用方应换 OS 级沙箱后端。
_MAX_CODE_CHARS = 64_000
_MAX_VALUE_CHARS = 64_000
_MAX_COLLECTION_ITEMS = 10_000
_MAX_INT_BITS = 8_192
_MAX_POWER_EXPONENT = 10_000


@dataclass(frozen=True)
class SandboxResult:
    """一次沙箱执行的结果:产出文本 + 来源沙箱名(provenance)+ 返回码 + 资源记账 + 失败原因。

    returncode == 0 表示成功;非 0 表示代码在沙箱内失败 / 触限 / 被拒(错误【被沙箱容住】并如实
    上报,而非以 Python 异常冒泡——沙箱的语义正是「容住失败」)。error 携带一个短失败原因码
    (disallowed / limit_exceeded / error / …),output 携带代码自己的产出或诊断文本。
    """

    sandbox: str
    output: str
    returncode: int = 0
    usage: ResourceUsage = field(default_factory=ResourceUsage)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """这次执行是否成功(返回码为 0)。"""
        return self.returncode == 0


@runtime_checkable
class Sandbox(Protocol):
    """sandbox 协议:有名字;隔离执行一段代码 / 命令,拿回带 provenance + 资源记账的结果。

    run 的 env 是执行环境的变量绑定(in-process:表达式里的自由名;subprocess/container:进程
    环境变量),可选;timeout 是墙钟超时的便捷入口(不为 None 则覆盖 limits.timeout_seconds)。
    """

    name: str

    def run(
        self,
        code: str,
        *,
        timeout: float | None = None,
        limits: Limits | None = None,
        env: object | None = None,
    ) -> SandboxResult: ...


def _effective_limits(timeout: float | None, limits: Limits | None) -> Limits:
    """把便捷入口 timeout 折叠进 limits;二者皆缺则退回 DEFAULT_LIMITS。"""
    base = limits if limits is not None else DEFAULT_LIMITS
    if timeout is not None:
        base = Limits(
            timeout_seconds=timeout,
            max_output_chars=base.max_output_chars,
            max_ops=base.max_ops,
        )
    return base


# ---- InProcessSandbox:受限白名单求值器(离线确定性默认)---------------------------------------

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}
_CMP_OPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
# 白名单纯函数:只放【无副作用、不触碰 IO / 反射】的内建。绝不含 open / eval / exec / __import__ /
# getattr / range(range 能被 sum(range(1e9)) 绕开节点预算做 DoS,故一并排除,只留有界容器函数)。
_SAFE_BUILTINS: dict[str, Callable[..., Any]] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "sorted": sorted,
    "repr": repr,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
}


class _Disallowed(Exception):
    """求值撞到白名单外的构造(Import / Attribute / 任意 Call / 未知 Name / 未知运算符)。"""


class _LimitExceeded(Exception):
    """求值超出某个资源上限(节点预算 max_ops)。"""


def _plain_int(value: Any) -> bool:
    """bool 是 int 子类，但不按大整数运算参与预测。"""
    return type(value) is int


def _sequence_repeat_operands(
    left: Any, right: Any
) -> tuple[str | bytes | list[Any] | tuple[Any, ...] | None, int | None]:
    """规整 ``sequence * int`` / ``int * sequence`` 两种重复形状。"""
    sequence_types = {str, bytes, list, tuple}
    if type(left) in sequence_types and _plain_int(right):
        return left, right
    if _plain_int(left) and type(right) in sequence_types:
        return right, left
    return None, None


def _bounded_render_cost(
    value: Any,
    remaining: int = _MAX_VALUE_CHARS,
    stack: set[int] | None = None,
) -> int:
    """保守估算 ``str(value)`` 物化成本，并递归拒绝非 JSON-like / 循环巨值。"""
    stack = stack if stack is not None else set()

    def charge(cost: int) -> int:
        if cost > remaining:
            raise _LimitExceeded(f"值的渲染成本超过 {_MAX_VALUE_CHARS}")
        return cost

    value_type = type(value)
    if value is None or value_type is bool:
        return charge(5)
    if value_type is int:
        if value.bit_length() > _MAX_INT_BITS:
            raise _LimitExceeded(f"整数位数超过 {_MAX_INT_BITS}")
        return charge((value.bit_length() * 3) // 10 + 3)
    if value_type is float:
        if not math.isfinite(value):
            raise _LimitExceeded("不允许非有限浮点值")
        return charge(32)
    if value_type is complex:
        if not (math.isfinite(value.real) and math.isfinite(value.imag)):
            raise _LimitExceeded("不允许非有限复数值")
        return charge(70)
    if value_type is str:
        return charge(len(value) + 2)
    if value_type is bytes:
        return charge(len(value) * 4 + 3)
    if value_type is slice:
        total = 8
        for part in (value.start, value.stop, value.step):
            total += _bounded_render_cost(part, remaining - total, stack)
        return charge(total)
    if value_type not in {list, tuple, set, frozenset, dict}:
        raise _Disallowed("只允许 JSON-like 标量/容器值，不允许自定义对象")
    if len(value) > _MAX_COLLECTION_ITEMS:
        raise _LimitExceeded(f"容器元素数超过 {_MAX_COLLECTION_ITEMS}")

    identity = id(value)
    if identity in stack:
        raise _Disallowed("不允许循环引用容器")
    stack.add(identity)
    try:
        total = 5 if value_type in {set, frozenset} and not value else 2
        items = value.items() if value_type is dict else value
        for item in items:
            if value_type is dict:
                key, child = item
                total += _bounded_render_cost(key, remaining - total, stack) + 2
                total += _bounded_render_cost(child, remaining - total, stack) + 2
            else:
                total += _bounded_render_cost(item, remaining - total, stack) + 2
            charge(total)
        return charge(total)
    finally:
        stack.remove(identity)


class _Evaluator:
    """一棵【纯表达式白名单 AST】的递归求值器,带节点预算(确定性 CPU 代理)。

    只认无副作用节点:字面量 / 名字(仅取自 env)/ 算术·比较·布尔运算 / 容器 / 下标 / 白名单
    函数调用 / f-string。任何 Import / Attribute(挡 .__class__ 逃逸与 os.system 等)/ 任意可调用
    / 非白名单 Name(挡 open / __import__)一律拒绝——【构造即保证】无网络出口、无文件系统逃逸。
    """

    def __init__(self, env: dict[str, object], max_ops: int | None) -> None:
        self._env = env
        self._max_ops = max_ops
        self.ops = 0

    def eval(self, node: ast.AST) -> Any:
        self.ops += 1
        if self._max_ops is not None and self.ops > self._max_ops:
            raise _LimitExceeded(f"求值节点数超过 max_ops={self._max_ops}")
        handler = _HANDLERS.get(type(node))
        if handler is None:
            raise _Disallowed(f"不允许的表达式节点:{type(node).__name__}")
        value = handler(self, node)
        self._ensure_bounded(value)
        return value

    @staticmethod
    def _ensure_bounded(value: Any) -> None:
        """递归拒绝巨值，确保最终 ``str(value)`` 之前已有总预算。"""
        _bounded_render_cost(value)

    # 各节点处理器(签名统一 (self, node) -> Any)。

    def _constant(self, node: ast.Constant) -> Any:
        return node.value

    def _name(self, node: ast.Name) -> Any:
        if node.id in self._env:
            return self._env[node.id]
        raise _Disallowed(f"未绑定的名字:{node.id!r}(仅允许 env 提供的变量)")

    def _binop(self, node: ast.BinOp) -> Any:
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise _Disallowed(f"不允许的二元运算符:{type(node.op).__name__}")
        left = self.eval(node.left)
        right = self.eval(node.right)
        self._guard_expensive_binop(node.op, left, right)
        return op(left, right)

    @staticmethod
    def _guard_expensive_binop(op: ast.operator, left: Any, right: Any) -> None:
        """在物化结果前拒绝可预测的巨型幂、重复与拼接。"""
        if isinstance(op, ast.Pow) and isinstance(right, int) and not isinstance(right, bool):
            if abs(right) > _MAX_POWER_EXPONENT:
                raise _LimitExceeded(f"幂指数绝对值超过 {_MAX_POWER_EXPONENT}")
            if right >= 0 and isinstance(left, int) and not isinstance(left, bool):
                predicted_bits = max(1, left.bit_length()) * right
                if predicted_bits > _MAX_INT_BITS:
                    raise _LimitExceeded(f"幂运算结果位数超过 {_MAX_INT_BITS}")

        if isinstance(op, ast.Mult):
            sequence, count = _sequence_repeat_operands(left, right)
            if sequence is not None and count is not None:
                limit = (
                    _MAX_VALUE_CHARS
                    if isinstance(sequence, (str, bytes))
                    else _MAX_COLLECTION_ITEMS
                )
                if count > 0 and len(sequence) > limit // count:
                    raise _LimitExceeded(f"序列重复结果大小超过 {limit}")
            if _plain_int(left) and _plain_int(right):
                if left.bit_length() + right.bit_length() > _MAX_INT_BITS + 1:
                    raise _LimitExceeded(f"整数乘法结果位数超过 {_MAX_INT_BITS}")

        if isinstance(op, ast.Add) and type(left) is type(right):
            if isinstance(left, (str, bytes)) and len(left) + len(right) > _MAX_VALUE_CHARS:
                raise _LimitExceeded(f"文本拼接结果长度超过 {_MAX_VALUE_CHARS}")
            if isinstance(left, (list, tuple)) and len(left) + len(right) > _MAX_COLLECTION_ITEMS:
                raise _LimitExceeded(f"容器拼接结果元素数超过 {_MAX_COLLECTION_ITEMS}")

        # Python 的字符串 % 格式支持超大宽度（如 "%1000000000s"），少量 AST 即可分配巨量内存。
        if isinstance(op, ast.Mod) and isinstance(left, (str, bytes)):
            raise _Disallowed("不允许字符串 % 格式化")

    def _unaryop(self, node: ast.UnaryOp) -> Any:
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise _Disallowed(f"不允许的一元运算符:{type(node.op).__name__}")
        return op(self.eval(node.operand))

    def _boolop(self, node: ast.BoolOp) -> Any:
        # 短路求值(and / or),语义与 Python 一致。
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self.eval(value)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = self.eval(value)
            if result:
                return result
        return result

    def _compare(self, node: ast.Compare) -> Any:
        left = self.eval(node.left)
        for op_node, right_node in zip(node.ops, node.comparators, strict=True):
            op = _CMP_OPS.get(type(op_node))
            if op is None:
                raise _Disallowed(f"不允许的比较运算符:{type(op_node).__name__}")
            right = self.eval(right_node)
            if not op(left, right):
                return False
            left = right
        return True

    def _ifexp(self, node: ast.IfExp) -> Any:
        return self.eval(node.body) if self.eval(node.test) else self.eval(node.orelse)

    def _list(self, node: ast.List) -> Any:
        return [self.eval(e) for e in node.elts]

    def _tuple(self, node: ast.Tuple) -> Any:
        return tuple(self.eval(e) for e in node.elts)

    def _set(self, node: ast.Set) -> Any:
        return {self.eval(e) for e in node.elts}

    def _dict(self, node: ast.Dict) -> Any:
        return {
            (self.eval(k) if k is not None else None): self.eval(v)
            for k, v in zip(node.keys, node.values, strict=True)
        }

    def _subscript(self, node: ast.Subscript) -> Any:
        return self.eval(node.value)[self.eval(node.slice)]

    def _slice(self, node: ast.Slice) -> Any:
        lower = self.eval(node.lower) if node.lower is not None else None
        upper = self.eval(node.upper) if node.upper is not None else None
        step = self.eval(node.step) if node.step is not None else None
        return slice(lower, upper, step)

    def _call(self, node: ast.Call) -> Any:
        # 只允许「白名单内建函数」的直接具名调用;任何其它可调用(方法 / lambda / 变量)一律拒绝。
        if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_BUILTINS:
            raise _Disallowed("只允许调用白名单内建函数(len/str/int/…),不允许任意调用")
        func = _SAFE_BUILTINS[node.func.id]
        args = [self.eval(a) for a in node.args]
        kwargs = {kw.arg: self.eval(kw.value) for kw in node.keywords if kw.arg is not None}
        return func(*args, **kwargs)

    def _joinedstr(self, node: ast.JoinedStr) -> Any:
        parts: list[str] = []
        total_chars = 0
        for part_node in node.values:
            part = self.eval(part_node)
            if type(part) is not str:
                raise _Disallowed("f-string 片段必须是普通 str")
            if len(part) > _MAX_VALUE_CHARS - total_chars:
                raise _LimitExceeded(f"f-string 结果长度超过 {_MAX_VALUE_CHARS}")
            parts.append(part)
            total_chars += len(part)
        return "".join(parts)

    def _formattedvalue(self, node: ast.FormattedValue) -> Any:
        value = self.eval(node.value)
        # 支持 !r / !s / !a 转换;format_spec 不支持(保持薄),按 str 呈现。
        if node.conversion == ord("r"):
            return repr(value)
        if node.conversion == ord("a"):
            return ascii(value)
        return str(value)


# 节点类型 -> 处理器方法(白名单闭集:不在此表即 _Disallowed)。
_HANDLERS: dict[type[ast.AST], Callable[[_Evaluator, Any], Any]] = {
    ast.Constant: _Evaluator._constant,
    ast.Name: _Evaluator._name,
    ast.BinOp: _Evaluator._binop,
    ast.UnaryOp: _Evaluator._unaryop,
    ast.BoolOp: _Evaluator._boolop,
    ast.Compare: _Evaluator._compare,
    ast.IfExp: _Evaluator._ifexp,
    ast.List: _Evaluator._list,
    ast.Tuple: _Evaluator._tuple,
    ast.Set: _Evaluator._set,
    ast.Dict: _Evaluator._dict,
    ast.Subscript: _Evaluator._subscript,
    ast.Slice: _Evaluator._slice,
    ast.Call: _Evaluator._call,
    ast.JoinedStr: _Evaluator._joinedstr,
    ast.FormattedValue: _Evaluator._formattedvalue,
}


def _format_output(value: Any) -> str:
    """把求值结果规整成文本:整数值的 float 去掉多余 .0,其余 str()。"""
    if type(value) is float and value.is_integer():
        return str(int(value))
    return str(value)


class InProcessSandbox:
    """离线确定性默认沙箱:把 code 当【纯表达式白名单小语言】在进程内求值,零网络、零文件系统。

    隔离由 AST 白名单【构造即保证】:拒绝 Import / Attribute / 任意 Call / 非白名单 Name,故代码
    无从 import socket / open 文件 / 反射逃逸。资源上限:max_ops 限求值节点数(确定性 CPU 代理)、
    max_output_chars 限产出。失败(语法错 / 被拒 / 触限 / 求值异常)一律【容住】为非 0 SandboxResult
    (带失败原因码),绝不以 Python 异常冒泡。
    """

    name = "in_process"

    def run(
        self,
        code: str,
        *,
        timeout: float | None = None,
        limits: Limits | None = None,
        env: object | None = None,
    ) -> SandboxResult:
        eff = _effective_limits(timeout, limits)
        started = time.perf_counter()
        evaluator = _Evaluator({}, eff.max_ops)
        if type(code) is not str:
            return self._fail("disallowed", "code 必须是普通 str", evaluator, started)
        if env is not None and type(env) is not dict:
            return self._fail("disallowed", "env 必须是普通 dict", evaluator, started)
        namespace = {} if env is None else env.copy()
        if any(type(name) is not str for name in namespace):
            return self._fail("disallowed", "env 的键必须是普通 str", evaluator, started)
        try:
            _bounded_render_cost(namespace)
        except _Disallowed as exc:
            return self._fail("disallowed", str(exc), evaluator, started)
        except _LimitExceeded as exc:
            return self._fail("limit_exceeded", str(exc), evaluator, started)
        evaluator = _Evaluator(namespace, eff.max_ops)
        if len(code) > _MAX_CODE_CHARS:
            return self._fail(
                "limit_exceeded",
                f"代码长度超过 {_MAX_CODE_CHARS}",
                evaluator,
                started,
            )
        try:
            tree = ast.parse(code, mode="eval")
        except (SyntaxError, ValueError, RecursionError) as exc:
            return self._fail("syntax", f"语法错误:{exc}", evaluator, started)
        try:
            value = evaluator.eval(tree.body)
        except _Disallowed as exc:
            return self._fail("disallowed", str(exc), evaluator, started)
        except _LimitExceeded as exc:
            return self._fail("limit_exceeded", str(exc), evaluator, started)
        except Exception as exc:  # noqa: BLE001 —— 沙箱容住代码自身的运行时错误,如实上报
            return self._fail("error", f"{type(exc).__name__}: {exc}", evaluator, started)

        output = _format_output(value)
        # 输出上限:超出即判失败(任何后端都能事后度量,故这是最中立的上限)。
        if eff.max_output_chars is not None and len(output) > eff.max_output_chars:
            truncated = output[: eff.max_output_chars]
            return SandboxResult(
                sandbox=self.name,
                output=truncated,
                returncode=1,
                usage=self._usage(evaluator, started, len(truncated)),
                error="limit_exceeded",
            )
        return SandboxResult(
            sandbox=self.name,
            output=output,
            usage=self._usage(evaluator, started, len(output)),
        )

    def _fail(
        self, reason: str, message: str, evaluator: _Evaluator, started: float
    ) -> SandboxResult:
        return SandboxResult(
            sandbox=self.name,
            output=message,
            returncode=1,
            usage=self._usage(evaluator, started, len(message)),
            error=reason,
        )

    @staticmethod
    def _usage(evaluator: _Evaluator, started: float, output_chars: int) -> ResourceUsage:
        return ResourceUsage(
            ops=evaluator.ops,
            output_chars=output_chars,
            wall_seconds=time.perf_counter() - started,
        )


def load_container_sdk() -> Any:
    """延迟 import 容器 SDK;未装 [sandbox] extra 时给「pip install spineagent[sandbox]」友好报错。"""
    return lazy_extra_import(_CONTAINER_SDK_MODULE, pkg="spineagent", extra="sandbox")


def _make_subprocess_sandbox(**kwargs: Any) -> Sandbox:
    # 真实硬隔离后端(子进程 + OS 级 namespaces / seccomp / rlimit)留待使用者按其平台接入:
    # 纯 subprocess 无法跨平台保证「无网络出口」,接了却过不了该不变量即是自欺,故给明确 SeamError。
    raise SeamError(
        "真实 subprocess 沙箱留待使用者按其平台接入(子进程 + OS 级隔离:namespaces / seccomp / "
        "rlimit,须真正封住网络出口与文件系统逃逸),并注册进 sandboxes 的 'subprocess' 位;"
        "本壳只提供缝 + 离线确定性默认 InProcessSandbox。"
    )


def _make_container_sandbox(**kwargs: Any) -> Sandbox:
    # 缺 [sandbox] extra -> 友好 ImportError(离线默认路径永远不会走到这)。
    sdk = load_container_sdk()
    raise SeamError(
        f"真实 container 沙箱留待装了 spineagent[sandbox] 的使用者按 {sdk.__name__!r} 接入;"
        "本壳只提供缝 + 离线确定性默认 InProcessSandbox。"
    )


# 缝注册表:一个 spec 选实现(默认 in_process 离线确定性默认;subprocess / container 走真实硬隔离
# 后端接入)。第三方亦可经 entry-point group "corespine.sandbox" 装包扩展自己的沙箱后端。
sandboxes: Registry[Sandbox] = Registry("sandbox")
sandboxes.register("in_process", lambda **kw: InProcessSandbox(**kw))
sandboxes.register("subprocess", _make_subprocess_sandbox)
sandboxes.register("container", _make_container_sandbox)
