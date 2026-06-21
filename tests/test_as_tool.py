"""agent-as-tool 合约:把 Agent 桥成 Tool + 分层督导式多 agent(supervisor → sub-agents)。"""

from agentspine.agent.agent import FunctionAgent
from agentspine.agent.as_tool import AgentTool
from agentspine.agent.policy import SyntaxToolPolicy
from agentspine.agent.tool_using import ToolUsingAgent
from agentspine.tools.tool import CalcTool, Tool


def test_agent_tool_bridges_an_agent():
    tool = AgentTool(FunctionAgent("sub", lambda t: f"sub:{t}"))
    assert isinstance(tool, Tool)
    result = tool.run("hi")
    assert result.tool == "sub"  # provenance 默认取子 agent 名
    assert result.output == "sub:hi"


def test_agent_tool_name_override():
    tool = AgentTool(FunctionAgent("inner", lambda t: t), name="delegate")
    assert tool.name == "delegate"
    assert tool.run("x").tool == "delegate"


def test_supervisor_delegates_to_a_subagent():
    # 督导 agent 通过工具调用把子任务派给一个专精子 agent(分层多 agent)。
    researcher = FunctionAgent("researcher", lambda t: f"[研究] {t}")
    supervisor = ToolUsingAgent("supervisor", SyntaxToolPolicy(), [AgentTool(researcher)])
    result = supervisor.step("researcher: 海平面上升")
    assert result.agent == "supervisor"
    assert "[研究] 海平面上升" in result.output


def test_supervisor_delegates_to_a_tool_using_subagent():
    # 嵌套:子 agent 自己也用工具——督导把 "calc: 2+3" 派给会算术的子 agent,层层跑通。
    calculator = ToolUsingAgent("calculator", SyntaxToolPolicy(), [CalcTool()])
    supervisor = ToolUsingAgent("supervisor", SyntaxToolPolicy(), [AgentTool(calculator)])
    result = supervisor.step("calculator: calc: 2+3")
    assert "5" in result.output


def test_supervisor_routes_among_multiple_subagents():
    upper = FunctionAgent("upper", lambda t: t.upper())
    rev = FunctionAgent("rev", lambda t: t[::-1])
    supervisor = ToolUsingAgent(
        "supervisor", SyntaxToolPolicy(), [AgentTool(upper), AgentTool(rev)]
    )
    # 点名 rev:路由到反转子 agent(而非第一个 upper)。
    assert "cba" in supervisor.step("rev: abc").output
