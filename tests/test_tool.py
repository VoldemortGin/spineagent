"""tool 合约:echo / calc 派发 + 结果带 provenance + 算术求值安全 + 缝注册表。"""

import threading

import pytest

from spineagent.tools.tool import CalcTool, EchoTool, Tool, ToolResult, tool_registry


def test_echo_tool_returns_input_with_provenance():
    tool = EchoTool()
    result = tool.run("hello")
    assert isinstance(result, ToolResult)
    assert result.output == "hello"
    assert result.tool == "echo"  # provenance
    assert isinstance(tool, Tool)


@pytest.mark.parametrize(
    ("expr", "expected"),
    [("1+1", "2"), ("2*(3+4)", "14"), ("10/4", "2.5"), ("-3 + 5", "2"), ("2**5", "32")],
)
def test_calc_tool_evaluates_arithmetic(expr, expected):
    result = CalcTool().run(expr)
    assert result.output == expected
    assert result.tool == "calc"  # provenance


def test_calc_tool_rejects_non_arithmetic_code():
    # 只认白名单算术节点;函数调用 / 名字 一律拒绝(绝不触碰任意代码执行)。
    with pytest.raises(ValueError):
        CalcTool().run("__import__('os').system('echo hi')")


def test_registry_makes_builtin_tools():
    assert isinstance(tool_registry.make("echo"), EchoTool)
    assert isinstance(tool_registry.make("calc"), CalcTool)
    # 大小写 / 留白不敏感(corespine Registry 归一)。
    assert isinstance(tool_registry.make("  CALC "), CalcTool)


def test_registry_lists_builtin_names():
    names = tool_registry.names()
    assert "echo" in names and "calc" in names


def test_registry_made_tool_runs_with_provenance():
    result = tool_registry.make("calc").run("1+1")
    assert result.output == "2"
    assert result.tool == "calc"


def test_registry_uses_corespine_tool_entry_point_group():
    # 第三方在该 group 下注册即被自动发现(机制由 corespine Registry 提供并已自测)。
    assert tool_registry.group == "corespine.tool"


def test_registry_unknown_spec_lists_available():
    with pytest.raises(ValueError) as ei:
        tool_registry.make("nope")
    msg = str(ei.value)
    assert "echo" in msg and "calc" in msg


def test_calc_tool_division_by_zero_raises():
    # 除零透传 ZeroDivisionError(求值层不吞,不伪造结果)。
    with pytest.raises(ZeroDivisionError):
        CalcTool().run("1/0")


def test_calc_tool_accepts_bare_bool_constant():
    # 特征化:bool 是 int 子类型,被白名单数字常量接纳(已知怪癖,非改动诉求)。
    assert CalcTool().run("True").output == "True"


def test_calc_tool_rejects_string_literal_constant():
    # 非数字常量(字符串字面量)被拒(只认数字常量)。
    with pytest.raises(ValueError):
        CalcTool().run("'abc'")


# ---- CalcTool DoS 上限:幂 / 位移 / 乘法位数 / 长度 / 嵌套深度一律立即受控报错 -----------------


def _run_guarded(expr: str) -> BaseException | None:
    """在守护线程里跑 CalcTool;5 秒没返回即判挂死(只作防挂死兜底,断言靠异常类型)。"""
    outcome: list[BaseException | None] = []

    def target() -> None:
        try:
            CalcTool().run(expr)
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 — 记录后在主线程断言
            outcome.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(5)
    assert outcome, f"CalcTool 对 {expr[:30]!r} 未在受控时间内返回"
    return outcome[0]


@pytest.mark.parametrize(
    "expr",
    [
        "9**9**9",
        "2**(2**30)",
        "1<<10**9",
        "(9**999)*(9**999)*(9**999)*(9**999)",
        "1+" * 5000 + "1",
        "-(" * 150 + "1" + ")" * 150,
        "-" * 500 + "1",
    ],
)
def test_calc_tool_bounds_expensive_expressions(expr):
    exc = _run_guarded(expr)
    assert isinstance(exc, ValueError), f"应受控地抛 ValueError,实得 {exc!r}"


def test_calc_tool_deep_bare_parentheses_fail_fast():
    # 纯括号不产生 AST 节点:150 层照常得 1;超过解析器上限则立即抛语法错(受控,不挂死)。
    assert CalcTool().run("(" * 150 + "1" + ")" * 150).output == "1"
    exc = _run_guarded("(" * 300 + "1" + ")" * 300)
    assert isinstance(exc, (SyntaxError, ValueError))


def test_calc_tool_still_handles_reasonable_powers():
    assert CalcTool().run("2**100").output == str(2**100)
    assert CalcTool().run("(1+2)*(3+4)").output == "21"
