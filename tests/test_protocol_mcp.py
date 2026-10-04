"""MCP 缝合约:离线 stub 满足 McpClient/McpServer + 回环调用 + 缺 [mcp] extra 友好报错。"""

import pytest

from spineagent.protocol.mcp.seam import (
    McpClient,
    McpClientTool,
    McpProtocolError,
    McpServer,
    McpTool,
    OfflineMcpStub,
    load_mcp_sdk,
    mcp_clients,
)


def test_offline_stub_satisfies_both_protocols():
    stub = OfflineMcpStub()
    assert isinstance(stub, McpClient)
    assert isinstance(stub, McpServer)


def test_offline_stub_register_list_call_loopback():
    stub = OfflineMcpStub()
    stub.register_tool(
        McpTool("upper", "uppercase a string"),
        lambda args: {"result": args["s"].upper()},
    )
    assert [t.name for t in stub.list_tools()] == ["upper"]
    assert stub.call_tool("upper", {"s": "hi"}) == {"result": "HI"}


def test_call_unknown_tool_raises():
    with pytest.raises(KeyError):
        OfflineMcpStub().call_tool("nope", {})


def test_registry_makes_offline_default():
    client = mcp_clients.make("offline")
    assert isinstance(client, McpClient)
    # 缝注册表把可用名列清(含离线 stub 与真实后端入口)。
    assert "offline" in mcp_clients.names()
    assert "real" in mcp_clients.names()


def test_real_backend_missing_extra_gives_friendly_error():
    # 默认离线环境未装 [mcp] extra:延迟 import 应给出可直接照做的安装指引。
    with pytest.raises(ImportError) as ei:
        load_mcp_sdk()
    assert "pip install spineagent[mcp]" in str(ei.value)


# ---- McpClientTool 桥(把 MCP client 的具名工具桥成 spineagent Tool)---------------------


def test_mcp_client_tool_custom_arg_key_and_result_key():
    # 自定义 arg_key / result_key:run 把单串入参包成 {arg_key: arg},再取结果里的 result_key。
    stub = OfflineMcpStub()
    stub.register_tool(McpTool("up"), lambda args: {"out": args["text"].upper()})
    tool = McpClientTool("up", stub, arg_key="text", result_key="out")
    result = tool.run("hi")
    assert result.output == "HI"
    assert result.tool == "up"  # provenance


def test_mcp_client_tool_missing_result_key_is_not_swallowed():
    # 结果里缺默认 result_key("result")→ 缝的类型化错误(不再是裸 KeyError;适配层仍不吞)。
    stub = OfflineMcpStub()
    stub.register_tool(McpTool("x"), lambda args: {"wrong": 1})
    with pytest.raises(McpProtocolError):
        McpClientTool("x", stub).run("a")


def test_mcp_client_tool_non_str_result_is_stringified():
    # 非 str 结果统一 str() 化(ToolResult.output 恒为字符串)。
    stub = OfflineMcpStub()
    stub.register_tool(McpTool("n"), lambda args: {"result": 42})
    assert McpClientTool("n", stub).run("a").output == "42"


def test_mcp_client_tool_missing_result_key_is_typed_error():
    stub = OfflineMcpStub()
    stub.register_tool(McpTool("weird"), lambda args: {"unexpected": 1})
    with pytest.raises(McpProtocolError) as ei:
        McpClientTool("weird", stub).run("x")
    assert ei.value.code == "mcp.invalid_result"
