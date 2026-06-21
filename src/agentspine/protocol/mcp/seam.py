"""MCP(Model Context Protocol)缝:McpClient / McpServer 协议 + 离线回环 stub 默认。

家族缝的元模式:Protocol + 离线确定性默认 + Registry 工厂 + 真实后端经可选 extra 延迟 import。
默认路径【零网络、零重依赖】:OfflineMcpStub 在进程内回环——注册工具 / 列工具 / 调工具,
同时满足 McpClient 与 McpServer 两个协议,让「装上即可端到端跑」与测试都不需要任何外部进程。

真实官方 MCP SDK 仅在选用时,经 [mcp] extra 由 corespine.lazy_extra_import 延迟 import;
未装该 extra 时给出「pip install agentspine[mcp]」友好报错,而不是裸 ModuleNotFoundError。
本模块顶层【绝不】import 真实 SDK——import agentspine 不该拉入任何网络 SDK。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from corespine.errors import SeamError
from corespine.seam.registry import Registry, lazy_extra_import

from agentspine.tools.tool import ToolResult

# 真实官方 MCP SDK 的 import 名(装了 agentspine[mcp] 才有);默认离线路径绝不 import 它。
_MCP_SDK_MODULE = "mcp"

# 一个工具处理器:参数 dict 进、结果 dict 出(可序列化,与 MCP 调用语义对齐)。
ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class McpTool:
    """一个 MCP 工具的最小描述:名字 + 可选说明。"""

    name: str
    description: str = ""


@runtime_checkable
class McpClient(Protocol):
    """MCP client 协议:列出可用工具、按名带参调用一个工具。"""

    def list_tools(self) -> list[McpTool]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class McpServer(Protocol):
    """MCP server 协议:注册一个工具及其处理器、列出已注册工具。"""

    def register_tool(self, tool: McpTool, handler: ToolHandler) -> None: ...

    def tools(self) -> list[McpTool]: ...


class OfflineMcpStub:
    """离线回环 MCP:进程内注册 + 调用,零网络。同时满足 McpClient 与 McpServer。"""

    def __init__(self) -> None:
        self._tools: dict[str, McpTool] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register_tool(self, tool: McpTool, handler: ToolHandler) -> None:
        self._tools[tool.name] = tool
        self._handlers[tool.name] = handler

    def tools(self) -> list[McpTool]:
        return list(self._tools.values())

    def list_tools(self) -> list[McpTool]:
        return list(self._tools.values())

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._handlers:
            raise KeyError(f"未注册的 MCP 工具:{name!r}(已注册:{sorted(self._handlers)})")
        return self._handlers[name](arguments)


class McpClientTool:
    """跨缝适配器:把一个 MCP client 的具名工具桥成 agentspine Tool(实现 Tool 协议)。

    让「会用工具的 agent」(ToolUsingAgent)能透过 MCP 调用远端 / 进程内的工具——把 run(arg)
    的单串入参包成 {arg_key: arg} 调 client.call_tool,再取结果里的 result_key 转字符串,带上
    provenance(ToolResult.tool = 工具名)。这正是 README 所说「把 ragspine RAG / 任意 MCP
    server 当作一个 Tool 在运行时组合」的通路:装一个实现了 McpClient 的适配器即可,零包依赖。

    最薄阻抗匹配:只做 str 单参 + 单键结果映射;复杂多参 schema 留待真实接入时再长(rule of three)。
    """

    def __init__(
        self,
        name: str,
        client: McpClient,
        *,
        arg_key: str = "input",
        result_key: str = "result",
    ) -> None:
        self.name = name
        self._client = client
        self._arg_key = arg_key
        self._result_key = result_key

    def run(self, arg: str) -> ToolResult:
        result = self._client.call_tool(self.name, {self._arg_key: arg})
        return ToolResult(tool=self.name, output=str(result[self._result_key]))


def load_mcp_sdk() -> Any:
    """延迟 import 真实 MCP SDK;未装 [mcp] extra 时给「pip install agentspine[mcp]」友好报错。"""
    return lazy_extra_import(_MCP_SDK_MODULE, pkg="agentspine", extra="mcp")


def _make_real_client(**kwargs: Any) -> McpClient:
    # 缺 [mcp] extra -> 友好 ImportError(离线默认路径永远不会走到这)。
    sdk = load_mcp_sdk()
    # 装了 extra 但适配器尚未接入:家族统一 SeamError(「缝槽存在但真实实现未接入」)。
    raise SeamError(
        f"真实 MCP client 适配器留待装了 agentspine[mcp] 的使用者按 {sdk.__name__!r} "
        "官方 SDK 接入;本壳只提供缝 + 离线 stub。"
    )


# 缝注册表:一个 spec 选实现(默认 offline 离线 stub;real 走延迟 import 的真实 SDK)。
mcp_clients: Registry[McpClient] = Registry("mcp_client")
mcp_clients.register("offline", lambda **kw: OfflineMcpStub(**kw))
mcp_clients.register("real", _make_real_client)
