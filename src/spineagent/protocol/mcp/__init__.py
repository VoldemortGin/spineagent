"""spineagent.protocol.mcp —— MCP 缝:McpClient / McpServer 协议 + 离线回环 stub。"""

from spineagent.protocol.mcp.seam import (
    McpClient,
    McpServer,
    McpTool,
    OfflineMcpStub,
    ToolHandler,
    load_mcp_sdk,
    mcp_clients,
)

__all__ = [
    "McpClient",
    "McpServer",
    "McpTool",
    "ToolHandler",
    "OfflineMcpStub",
    "mcp_clients",
    "load_mcp_sdk",
]
