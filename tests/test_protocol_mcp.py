"""MCP 缝合约:离线 stub 满足 McpClient/McpServer + 回环调用 + 缺 [mcp] extra 友好报错。"""

import pytest

from spineagent.protocol.mcp.seam import (
    McpClient,
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
