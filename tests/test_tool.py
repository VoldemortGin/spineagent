"""tool 合约:echo / calc 派发 + 结果带 provenance + 算术求值安全 + 缝注册表。"""

import pytest

from agentspine.tools.tool import CalcTool, EchoTool, Tool, ToolResult, tool_registry


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
