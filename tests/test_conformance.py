"""conformance 合约:用 corespine harness 把本包的不变量绑成参数化套件 + 检出泄露违反。

机制由 corespine.ConformanceSuite 提供(实现 × 不变量 笛卡尔积);保证由 agentspine 绑定
(ADR 0001 D6)。这里把三类实现各喂进自己的不变量包:
  - 5 个 agent 实现(llm / function / tool_using / a2a_adapter / chain)× 3 条 agent 不变量;
  - 4 个 tool 实现(echo / calc / mcp_client_tool / agent_tool)× 2 条 tool 不变量;
  - 1 个 policy 实现(syntax)× 4 条 tool-policy 不变量。
跨原语适配器(McpClientTool / A2AAgentAdapter / AgentTool)与 ToolUsingAgent 都【复用既有不变量
包】跑全套——元模式红利:它们号称是 Tool / Agent,就必须过 Tool / Agent 的全部保证。
再用一个故意把任务正文写进 trace 的「泄露 agent」证明:隐私不变量格子会被 run() 标红。
"""

import pytest
from corespine.conformance.harness import ConformanceSuite
from corespine.llm.provider import MockProvider

from agentspine.agent.agent import AgentResult, FunctionAgent, LlmAgent
from agentspine.agent.as_tool import AgentTool
from agentspine.agent.policy import SyntaxToolPolicy
from agentspine.agent.tool_using import ToolUsingAgent
from agentspine.conformance import AGENT_INVARIANTS, POLICY_INVARIANTS, TOOL_INVARIANTS
from agentspine.orchestration.chain import ChainAgent
from agentspine.protocol.a2a.seam import A2AAgentAdapter, OfflineA2AStub
from agentspine.protocol.mcp.seam import McpClientTool, McpTool, OfflineMcpStub
from agentspine.tools.tool import CalcTool, EchoTool


def _echo_mcp_tool() -> McpClientTool:
    """构造一个对任意 arg 回显的 MCP 工具桥(供 TOOL_INVARIANTS 用 '1+1' 驱动)。"""
    stub = OfflineMcpStub()
    stub.register_tool(McpTool("mcp_echo"), lambda args: {"result": args["input"]})
    return McpClientTool("mcp_echo", stub)


AGENT_SUITE = ConformanceSuite(
    {
        "llm": lambda: LlmAgent("llm", MockProvider()),
        "function": lambda: FunctionAgent("function", lambda task: f"done:{task}"),
        "tool_using": lambda: ToolUsingAgent("tool_using", SyntaxToolPolicy(), [CalcTool()]),
        "a2a_adapter": lambda: A2AAgentAdapter(OfflineA2AStub()),
        "chain": lambda: ChainAgent("chain", [FunctionAgent("a", lambda t: f"a:{t}")]),
    },
    AGENT_INVARIANTS,
)

TOOL_SUITE = ConformanceSuite(
    {
        "echo": EchoTool,
        "calc": CalcTool,
        "mcp_client_tool": _echo_mcp_tool,
        "agent_tool": lambda: AgentTool(FunctionAgent("sub", lambda t: f"sub:{t}")),
    },
    TOOL_INVARIANTS,
)

POLICY_SUITE = ConformanceSuite({"syntax": SyntaxToolPolicy}, POLICY_INVARIANTS)


@pytest.mark.parametrize(**AGENT_SUITE.parametrize_kwargs())
def test_agent_conformance(case):
    """每个 agent 实现 × 每条 agent 不变量 各跑一格(5 × 3 = 15 格全绿)。"""
    case()


@pytest.mark.parametrize(**TOOL_SUITE.parametrize_kwargs())
def test_tool_conformance(case):
    """每个 tool 实现 × 每条 tool 不变量 各跑一格(4 × 2 = 8 格全绿)。"""
    case()


@pytest.mark.parametrize(**POLICY_SUITE.parametrize_kwargs())
def test_policy_conformance(case):
    """每个 policy 实现 × 每条 tool-policy 不变量 各跑一格(1 × 4 = 4 格全绿)。"""
    case()


def test_conformance_detects_a_trace_payload_leak():
    """故意把任务正文写进 trace 的 agent:隐私不变量格子应被 run() 如实标红。"""

    class LeakyAgent:
        name = "leaky"

        def step(self, task, *, trace=None):
            if trace is not None:
                # 违规:把任务正文塞进 trace。InProcessPrivacyTraceSink.emit 会抛 TraceError。
                trace.emit("agent_step", text=task)
            return AgentResult(agent=self.name, output="ok")

    suite = ConformanceSuite({"leaky": LeakyAgent}, AGENT_INVARIANTS)
    results = suite.run()
    assert not suite.passed()
    failed = {r.invariant for r in results if not r.passed}
    # provenance / 产出两条它没违反;只在隐私 trace 那条踩雷。
    assert failed == {"step_traces_are_privacy_safe"}
