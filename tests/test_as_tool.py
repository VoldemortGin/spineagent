"""agent-as-tool 合约:把 Agent 桥成 Tool + 分层督导式多 agent(supervisor → sub-agents)。"""

from spineagent.agent.agent import FunctionAgent
from spineagent.agent.as_tool import AgentTool
from spineagent.agent.policy import SyntaxToolPolicy
from spineagent.agent.tool_using import ToolUsingAgent
from spineagent.tools.tool import CalcTool, Tool


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


def test_agent_tool_passes_usage_and_artifacts_to_the_calling_agent():
    from spineagent.agent.agent import AgentResult
    from spineagent.agent.artifact import ArtifactRef

    ref = ArtifactRef(sink="mem", key="k", name="r.txt", mime="text/plain", size=1, producer="sub")

    class Sub:
        name = "sub"

        def step(self, task, *, trace=None):
            usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
            return AgentResult("sub", "ok", usage=usage, artifacts=(ref,))

    tool = AgentTool(Sub())
    tr = tool.run("x")
    assert tr.usage == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    assert tr.artifacts == (ref,)
    supervisor = ToolUsingAgent("sup", SyntaxToolPolicy(), [tool])
    result = supervisor.step("sub: a\nsub: b")
    assert result.usage == {"prompt_tokens": 14, "completion_tokens": 6, "total_tokens": 20}
    assert result.artifacts == (ref, ref)
