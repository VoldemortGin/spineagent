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
    assert result.usage.ops < 10  # 第二个片段即拒绝，不遍历余下片段或物化完整结果


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
    assert result.usage.ops == 2  # Call + 实参 Name:节点预算与元素数无关


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
