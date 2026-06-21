"""跨缝适配器合约:McpClientTool(MCP 工具→Tool)、A2AAgentAdapter(A2AAgent→Agent)。"""

from corespine.observability.trace import InProcessPrivacyTraceSink

from agentspine.agent.agent import Agent
from agentspine.agent.policy import SyntaxToolPolicy
from agentspine.agent.tool_using import ToolUsingAgent
from agentspine.orchestration.coordinator import Coordinator
from agentspine.protocol.a2a.seam import A2AAgentAdapter, OfflineA2AStub
from agentspine.protocol.mcp.seam import McpClientTool, McpTool, OfflineMcpStub
from agentspine.tools.tool import Tool


def _upper_stub() -> OfflineMcpStub:
    stub = OfflineMcpStub()
    stub.register_tool(McpTool("upper"), lambda args: {"result": args["input"].upper()})
    return stub


def test_mcp_client_tool_bridges_an_mcp_tool():
    tool = McpClientTool("upper", _upper_stub())
    assert isinstance(tool, Tool)
    result = tool.run("hi")
    assert result.tool == "upper"  # provenance
    assert result.output == "HI"


def test_mcp_client_tool_custom_keys():
    stub = OfflineMcpStub()
    stub.register_tool(McpTool("rev"), lambda args: {"out": args["text"][::-1]})
    tool = McpClientTool("rev", stub, arg_key="text", result_key="out")
    assert tool.run("abc").output == "cba"


def test_mcp_tool_drives_a_tool_using_agent():
    # 端到端:把一个 MCP 工具桥成 Tool,交给会用工具的 agent 在循环里调用。
    agent = ToolUsingAgent("worker", SyntaxToolPolicy(), [McpClientTool("upper", _upper_stub())])
    result = agent.step("upper: hello")
    assert "HELLO" in result.output


def test_a2a_adapter_bridges_a_remote_agent():
    adapter = A2AAgentAdapter(OfflineA2AStub(name="remote", responder=lambda t: f"handled:{t}"))
    assert isinstance(adapter, Agent)
    assert adapter.name == "remote"
    result = adapter.step("ping")
    assert result.agent == "remote"  # provenance = remote.name
    assert result.output == "handled:ping"


def test_a2a_adapter_runs_in_coordinator():
    remote = A2AAgentAdapter(OfflineA2AStub(name="remote"))
    local = ToolUsingAgent("local", SyntaxToolPolicy(), [])
    coord = Coordinator([remote, local])
    results = coord.run_sequential("go")
    assert [r.agent for r in results] == ["remote", "local"]
    assert all(r.output for r in results)


def test_a2a_adapter_step_trace_is_privacy_safe():
    sink = InProcessPrivacyTraceSink()
    adapter = A2AAgentAdapter(OfflineA2AStub(name="remote"))
    adapter.step("机密任务正文 42", trace=sink)
    assert sink.codes() == ["agent_step"]
    for event in sink.events:
        assert all("机密任务" not in str(v) for v in event.fields.values())
