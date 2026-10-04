"""sandbox 缝的单元测试:InProcessSandbox 隔离不变量 + 资源上限 + provenance + 注册表 / 桩。

conformance 里已把「无网络出口 / 上限生效 / 结果带 provenance」参数化钉死(见 test_conformance.py);
这里补 InProcessSandbox 专属的行为断言(白名单语义、各失败原因码、env 绑定、real 后端桩)。
"""

import sys
from collections.abc import Iterator, Mapping

import pytest
from corespine.errors import SeamError

from spineagent.sandbox.seam import (
    DEFAULT_LIMITS,
    InProcessSandbox,
    Limits,
    ResourceUsage,
    SandboxResult,
    sandboxes,
)


def test_evaluates_arithmetic_with_provenance_and_accounting():
    result = InProcessSandbox().run("1 + 2 * 3")
    assert isinstance(result, SandboxResult)
    assert result.ok
    assert result.output == "7"
    assert result.sandbox == "in_process"  # provenance
    assert result.usage.ops > 0
    assert result.usage.output_chars == 1
    assert result.usage.wall_seconds >= 0.0


def test_integer_float_is_cleaned():
    assert InProcessSandbox().run("6 / 2").output == "3"


def test_env_bindings_are_the_only_names():
    result = InProcessSandbox().run("x + y", env={"x": 2, "y": 5})
    assert result.output == "7"


def test_custom_mapping_is_rejected_without_invoking_user_code():
    invoked = False

    class HostileMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            nonlocal invoked
            invoked = True
            raise AssertionError("custom mapping must not be queried")

        def __iter__(self) -> Iterator[str]:
            nonlocal invoked
            invoked = True
            raise AssertionError("custom mapping must not be iterated")

        def __len__(self) -> int:
            nonlocal invoked
            invoked = True
            raise AssertionError("custom mapping length must not be read")

    result = InProcessSandbox().run("x", env=HostileMapping())

    assert not result.ok
    assert result.error == "disallowed"
    assert not invoked


def test_custom_container_value_is_rejected_without_invoking_user_code():
    invoked = False

    class HostileList(list[object]):
        def __len__(self) -> int:
            nonlocal invoked
            invoked = True
            raise AssertionError("custom container length must not be read")

        def __iter__(self) -> Iterator[object]:
            nonlocal invoked
            invoked = True
            raise AssertionError("custom container must not be iterated")

        def __str__(self) -> str:
            nonlocal invoked
            invoked = True
            raise AssertionError("custom container must not be rendered")

    result = InProcessSandbox().run("x", env={"x": HostileList([1])})

    assert not result.ok
    assert result.error == "disallowed"
    assert not invoked


def test_unbound_name_is_refused():
    result = InProcessSandbox().run("nope")
    assert not result.ok
    assert result.error == "disallowed"


@pytest.mark.parametrize(
    "code",
    [
        "__import__('socket')",  # 网络出口
        "open('/etc/passwd')",  # 文件系统逃逸
        "(1).__class__",  # 反射逃逸
        "[].append(1)",  # 任意方法调用
        "eval('1')",  # 任意可调用
    ],
)
def test_isolation_refuses_escapes(code):
    result = InProcessSandbox().run(code)
    assert not result.ok, f"越权代码不该成功:{code}"
    assert result.error == "disallowed"


def test_whitelisted_builtins_work():
    assert InProcessSandbox().run("sum([1, 2, 3])").output == "6"
    assert InProcessSandbox().run("len('abcd')").output == "4"
    assert InProcessSandbox().run("max(3, 9, 1)").output == "9"


def test_fstring_renders():
    result = InProcessSandbox().run("f'hi {name}'", env={"name": "bob"})
    assert result.output == "hi bob"


def test_repeated_large_fstring_value_is_rejected_before_join():
    code = 'f"' + "{x}" * 1_000 + '"'
    result = InProcessSandbox().run(code, env={"x": "a" * 32_000})

    assert not result.ok
    assert result.error == "limit_exceeded"
    # 第二个片段即拒绝，不遍历余下片段或物化完整结果(ops 含按 32k 文本规模折算的工作量单位;
    # 走完 1000 个片段至少要上万单位)。
    assert result.usage.ops < 300


def test_syntax_error_is_contained():
    result = InProcessSandbox().run("1 +")
    assert not result.ok
    assert result.error == "syntax"


def test_runtime_error_is_contained():
    result = InProcessSandbox().run("1 / 0")
    assert not result.ok
    assert result.error == "error"
    assert "ZeroDivision" in result.output


def test_max_ops_limit_takes_effect():
    result = InProcessSandbox().run("1 + 1 + 1 + 1", limits=Limits(max_ops=1))
    assert not result.ok
    assert result.error == "limit_exceeded"


def test_max_output_chars_limit_takes_effect():
    result = InProcessSandbox().run("123456", limits=Limits(max_output_chars=2))
    assert not result.ok
    assert result.error == "limit_exceeded"
    assert result.output == "12"  # 截断到上限


@pytest.mark.parametrize(
    "code",
    [
        "'x' * 1000000000",
        "1000000000 * 'x'",
        "2 ** 1000000000",
        "[0] * 1000000000",
        "['x' * 60000] * 10000",
    ],
)
def test_high_cost_expression_is_rejected_before_materialization(code):
    result = InProcessSandbox().run(code)
    assert not result.ok
    assert result.error == "limit_exceeded"


def test_bounded_repetition_and_power_still_work():
    sandbox = InProcessSandbox()
    assert sandbox.run("'ab' * 3").output == "ababab"
    assert sandbox.run("2 ** 10").output == "1024"


def test_timeout_folds_into_limits_without_crashing():
    # 宽松的 timeout 折叠进 limits 做协作式 deadline,不该让正常求值失败。
    result = InProcessSandbox().run("1 + 1", timeout=1.0)
    assert result.ok and result.output == "2"


def test_default_limits_shape():
    assert DEFAULT_LIMITS.max_output_chars == 64_000
    assert isinstance(ResourceUsage(), ResourceUsage)


def test_registry_makes_in_process_default():
    sandbox = sandboxes.make("in_process")
    assert sandbox.run("2 * 2").output == "4"
    assert "in_process" in sandboxes.names()


def test_subprocess_backend_is_a_seam_stub():
    with pytest.raises(SeamError):
        sandboxes.make("subprocess")


def test_container_backend_errors_without_extra():
    # 未装 [sandbox] extra -> 友好 ImportError;装了但未接入 -> SeamError。二者皆非默认路径。
    with pytest.raises((ImportError, SeamError)):
        sandboxes.make("container")


# ---- 热路径 DoS 回归:按「运行期装饰次数」与「操作计数」断言,不靠墙钟 ------------------------


def _count_runtime_beartype_decorations(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """给每个已加载 spineagent 模块的 claw 装饰器挂计数:运行期每装饰一次嵌套函数就 +1。"""
    pytest.importorskip("beartype")
    counter = {"n": 0}
    for name, module in list(sys.modules.items()):
        decorate = getattr(module, "__beartype__", None)
        if not name.startswith("spineagent") or decorate is None:
            continue

        def counting(*args, _decorate=decorate, **kwargs):
            counter["n"] += 1
            return _decorate(*args, **kwargs)

        monkeypatch.setattr(module, "__beartype__", counting)
    return counter


def test_render_cost_check_does_not_redecorate_per_call(monkeypatch):
    # 修复前:_bounded_render_cost 的嵌套 charge 每次调用都被 beartype claw 重新装饰(约 1.5ms/次),
    # 2000 元素的 env 即触发上万次装饰。现在热路径上运行期装饰次数必须为 0。
    counter = _count_runtime_beartype_decorations(monkeypatch)
    result = InProcessSandbox().run("sorted(x)", env={"x": list(range(2000))})
    assert result.ok
    assert counter["n"] == 0
    # ops 现为工作量单位:sorted(x) 按 len(x) 计费、结果规模检查按元素计费(与运行期装饰次数无关)。
    assert 2_000 <= result.usage.ops < 10_000


def test_tool_loops_do_not_redecorate_per_call(monkeypatch):
    from spineagent.agent.function_calling import FunctionCallingAgent
    from spineagent.agent.policy import SyntaxToolPolicy
    from spineagent.agent.tool_using import ToolUsingAgent
    from spineagent.conformance import ScriptedToolCallProvider
    from spineagent.tools.function_tool import function_tool
    from spineagent.tools.tool import CalcTool

    @function_tool
    def double(value: str) -> str:
        return value * 2

    fc = FunctionCallingAgent(
        "fc", ScriptedToolCallProvider([("double", {"value": "a"})] * 5), [double]
    )
    tu = ToolUsingAgent("tu", SyntaxToolPolicy(), [CalcTool()])
    counter = _count_runtime_beartype_decorations(monkeypatch)
    fc.step("go")
    tu.step("calc: 1+1\ncalc: $prev * 3")
    InProcessSandbox().run("sum([1, 2, 3])", env={"x": {"k": [1, 2]}})
    assert counter["n"] == 0


# ---- 协作式超时:按节点检查 deadline,时钟可注入 ---------------------------------------------


class _StepClock:
    """每读一次前进 step 秒的假时钟(离线确定性)。"""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_timeout_is_enforced_cooperatively():
    clock = _StepClock(0.01)
    code = "[" + ",".join("1" for _ in range(100)) + "]"
    result = InProcessSandbox(clock=clock).run(code, timeout=0.05)
    assert not result.ok
    assert result.error == "limit_exceeded"
    assert result.usage.ops < 101  # 超时后立即停止求值,不再走完全部节点


def test_generous_timeout_does_not_interfere():
    result = InProcessSandbox(clock=_StepClock(0.001)).run("1 + 2", timeout=10.0)
    assert result.ok and result.output == "3"


def test_zero_timeout_never_succeeds():
    result = InProcessSandbox().run("1 + 1", timeout=0.0)
    assert not result.ok and result.error == "limit_exceeded"


def test_no_timeout_means_no_deadline():
    clock = _StepClock(1000.0)
    limits = Limits(timeout_seconds=None, max_output_chars=100, max_ops=100)
    assert InProcessSandbox(clock=clock).run("1 + 2", limits=limits).ok


# ---- 先验规模守卫:单个节点内部的代价必须由先验规则封顶(协作式 deadline 只是第二道闸)----------
# 协作式超时无法中断单个内建调用:round(1, -10**7) 在【一个】节点里计算 10**10**7。以下用例都在
# 「无 deadline + 冻结时钟」下跑,断言它们被【先验】拒绝(不靠计时);规模刻意调到毫秒量级。

_NO_DEADLINE = Limits(timeout_seconds=None, max_output_chars=64_000, max_ops=100_000)


def _frozen_clock() -> float:
    return 0.0


def _run_prior(code: str, env: dict[str, object] | None = None) -> SandboxResult:
    return InProcessSandbox(clock=_frozen_clock).run(code, limits=_NO_DEADLINE, env=env)


@pytest.mark.parametrize(
    "code",
    [
        "round(1, -5000)",  # 审查复现的放大器(缩小规模):int 的负 ndigits 在内部算 10**|ndigits|
        "round(1, ndigits=-5000)",  # 关键字形式同样拦
        "round(x, -5000)",
    ],
)
def test_round_ndigits_amplification_is_refused_before_call(code):
    result = _run_prior(code, env={"x": 1})
    assert not result.ok
    assert result.error == "limit_exceeded"
    assert result.usage.ops < 50  # 先验拒绝:没有真的去算 10**5000


def test_round_normal_usage_is_unaffected():
    sandbox = InProcessSandbox()
    assert sandbox.run("round(3.14159, 2)").output == "3.14"
    assert sandbox.run("round(1234, -2)").output == "1200"
    assert sandbox.run("round(2.5)").output == "2"


def test_int_from_overlong_digit_string_is_refused_before_conversion():
    # 十进制串 -> int 是超线性的;不依赖宿主进程可被改掉的 sys int_max_str_digits 全局设置。
    result = _run_prior("int(s)", env={"s": "1" * 5_000})
    assert result.error == "limit_exceeded"
    assert _run_prior("int('ff', 16)").output == "255"


def test_sum_only_accumulates_numbers():
    # 序列累加(sum(lists, []))是二次的:start 只接受数值。
    result = _run_prior("sum([[1], [2]], [])")
    assert not result.ok and result.error == "disallowed"
    assert _run_prior("sum([1, 2], 10)").output == "13"
    assert _run_prior("sum([1.5, 2])").output == "3.5"


_LIST_ENV = {"x": list(range(5_000))}
_TEXT_ENV = {"s": "ab" * 25_000}


@pytest.mark.parametrize(
    ("code", "env"),
    [
        ("max(" + ", ".join(["x"] * 50) + ")", _LIST_ENV),  # 内建实参按输入规模计费
        ("[" + ", ".join(["x == x"] * 30) + "]", _LIST_ENV),  # 比较的操作数按规模计费
        ("[" + ", ".join(["len(str(x))"] * 30) + "]", _LIST_ENV),  # str() 的实参按规模计费
        ("[" + ", ".join(["max(s)"] * 5) + "]", _TEXT_ENV),  # 迭代型内建对 str 按字符数计费
        ("[" + ", ".join(["[x][0][0]"] * 30) + "]", _LIST_ENV),  # 节点结果的规模检查也计费
    ],
)
def test_repeated_reference_to_large_value_is_charged_to_work_budget(code, env):
    # 修复前:反复引用同一个大值,每个节点都很「小」,却在节点内部做 O(len) 的工作,要到墙钟才停。
    result = _run_prior(code, env=env)
    assert not result.ok
    assert result.error == "limit_exceeded"


def test_large_builtin_work_is_counted_in_ops():
    result = _run_prior("sorted(x)[0]", env={"x": list(range(2_000))})
    assert result.ok and result.output == "0"
    assert result.usage.ops >= 2_000  # sorted(x) 记 len(x) 个工作量单位


def test_hard_work_cap_applies_even_without_max_ops():
    limits = Limits(timeout_seconds=None, max_output_chars=64_000, max_ops=None)
    code = "max(" + ", ".join(["x"] * 300) + ")"
    result = InProcessSandbox(clock=_frozen_clock).run(code, limits=limits, env=_LIST_ENV)
    assert not result.ok and result.error == "limit_exceeded"


class _ScriptedClock:
    """按预设序列返回时刻(耗尽后停在最后一个值)。"""

    def __init__(self, *values: float) -> None:
        self._values = list(values)

    def __call__(self) -> float:
        return self._values.pop(0) if len(self._values) > 1 else self._values[0]


def test_deadline_is_checked_after_each_node():
    # 单节点:求值前未超时,求值完成时已超时 -> 不得返回 ok=True(修复前节点跑完后不再检查)。
    clock = _ScriptedClock(0.0, 0.0, 10.0)
    result = InProcessSandbox(clock=clock).run("1", timeout=1.0)
    assert not result.ok
    assert result.error == "limit_exceeded"


def test_deeply_nested_env_is_contained_not_raised():
    value: object = 1
    for _ in range(800):
        value = [value]
    result = InProcessSandbox().run("1", env={"x": value})
    assert not result.ok and result.error == "limit_exceeded"


def test_skill_bundle_round_amplifier_is_refused(tmp_path):
    from spineagent.skills import SkillBundle, SkillError

    (tmp_path / "manifest.toml").write_text(
        'name = "dos"\ndescription = "d"\n[inputs]\ntype = "object"\n'
        '[inputs.properties.x]\ntype = "integer"\n',
        encoding="utf-8",
    )
    (tmp_path / "skill.py").write_text("round(x, -5000)", encoding="utf-8")
    skill = SkillBundle.load(tmp_path)
    with pytest.raises(SkillError) as ei:
        skill.invoke({"x": 1})
    assert ei.value.reason == "limit_exceeded"


# ---- 第三轮修改 5:按估算字节计的内存预算 / MemoryError 与 RecursionError 归一 / 语义错误 ----------------

_WIDE = "\U0010fffd"  # 非 BMP 字符:CPython 里每字符 4 字节


def _wide_list(count: int) -> str:
    return "len([" + ",".join([f"'{_WIDE}'*63990"] * count) + "])"


def test_review_r2_wide_strings_stop_mid_literal_within_the_memory_budget():
    # 复审:14 KB 代码造出 1400 个各 64k 字符的宽字符串(约 370 MB):列表字面量先全部求值,最后才做一次规模检查。
    budget = 2_000_000
    one_value = sys.getsizeof(_WIDE * 63990)  # 单个元素约 256 KB
    small = InProcessSandbox().run(
        _wide_list(40), limits=Limits(max_memory_bytes=budget, max_ops=None)
    )
    assert small.error == "limit_exceeded" and "内存" in small.output
    assert small.usage.memory_bytes <= budget + one_value  # 最多越过预算一个值
    # 在求值中途就停止:没有把 40 个元素都物化出来。
    assert small.usage.memory_bytes < 10 * one_value
    full = InProcessSandbox().run(
        _wide_list(40), limits=Limits(max_memory_bytes=10**8, max_ops=None)
    )
    assert small.usage.ops < full.usage.ops / 2


def test_default_memory_budget_applies_and_none_still_has_a_hard_cap():
    default = InProcessSandbox().run(_wide_list(200))
    assert default.error == "limit_exceeded"
    assert default.usage.memory_bytes <= DEFAULT_LIMITS.max_memory_bytes + 300_000
    unlimited = InProcessSandbox().run(
        _wide_list(700),
        limits=Limits(timeout_seconds=None, max_output_chars=None, max_ops=None),
    )
    assert unlimited.error == "limit_exceeded"
    assert unlimited.usage.memory_bytes <= 128 * 2**20 + 300_000  # 不可关闭的硬上限


def test_call_arguments_are_charged_as_they_are_evaluated():
    code = "max(" + ",".join([f"'{_WIDE}'*63990"] * 40) + ")"
    result = InProcessSandbox().run(code, limits=Limits(max_memory_bytes=1_000_000))
    assert result.error == "limit_exceeded"
    assert result.usage.memory_bytes < 10 * sys.getsizeof(_WIDE * 63990)


def test_work_budget_counts_bytes_not_characters():
    ascii_run = InProcessSandbox().run("len('a'*60000)")
    wide_run = InProcessSandbox().run(f"len('{_WIDE}'*60000)")
    assert ascii_run.ok and wide_run.ok
    assert wide_run.usage.ops >= ascii_run.usage.ops + 150  # 4 倍宽的字符串折算出约 4 倍单位


@pytest.mark.parametrize(
    "code",
    ["-" * 6000 + "1", "-" * 1000 + "1"],
    ids=["unary-6000", "unary-1000"],
)
def test_review_r2_memory_and_recursion_errors_are_contained(code):
    # 复审:'-'*6000+'1' 一类表达式让 MemoryError 冒泡出 run()(违反「失败一律容住」)。
    result = InProcessSandbox().run(code)
    assert not result.ok and result.error == "limit_exceeded"


def test_review_r2_dict_unpacking_is_correct_or_refused():
    # 复审:{**d} 得到 {None: d}、dict(**d) 得到 {} —— 静默算错。
    env = {"d": {"a": 1}, "e": {"b": 2}}
    assert (
        InProcessSandbox().run("{**d, 'c': 3, **e}", env=env).output == "{'a': 1, 'c': 3, 'b': 2}"
    )
    assert InProcessSandbox().run("{**x}", env={"x": [1]}).error == "disallowed"
    refused = InProcessSandbox().run("dict(**d)", env=env)
    assert refused.error == "disallowed" and "**" in refused.output


@pytest.mark.parametrize("terms", [250, 400, 2000])
def test_review_r2_long_left_associative_chains_evaluate(terms):
    # 复审:250 项以上的连加 1+…+400 报 RecursionError。
    result = InProcessSandbox().run("+".join(["1"] * terms))
    assert result.ok and result.output == str(terms)
    assert InProcessSandbox().run("-".join(["1"] * terms)).output == str(2 - terms)


def test_container_literal_itself_is_charged_even_when_elements_are_references():
    # 元素是 env 里已有值的引用(不是新物化的内存),容器字面量自身的指针数组仍要计入内存预算。
    code = "len([" + ",".join(["s"] * 2000) + "])"
    result = InProcessSandbox().run(code, env={"s": "x"})
    assert result.ok and result.usage.memory_bytes >= 2000 * 8
