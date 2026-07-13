"""sandbox 缝的单元测试:InProcessSandbox 隔离不变量 + 资源上限 + provenance + 注册表 / 桩。

conformance 里已把「无网络出口 / 上限生效 / 结果带 provenance」参数化钉死(见 test_conformance.py);
这里补 InProcessSandbox 专属的行为断言(白名单语义、各失败原因码、env 绑定、real 后端桩)。
"""

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


def test_timeout_folds_into_limits_without_crashing():
    # in-process 无法抢占同步求值,timeout 仅折叠 / 记录,不该让正常求值失败。
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
