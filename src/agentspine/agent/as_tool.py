"""把一个 Agent 暴露成 Tool:让一个 agent 把另一个 agent 当工具调用(分层 / 督导式多 agent)。

AgentTool 是把现有原语组合出【分层多 agent】的关键一块:一个督导 agent(ToolUsingAgent)通过
工具调用把子任务派给专精的子 agent——子 agent 也可以自己再用工具、再带子 agent,层层嵌套。
它与 McpClientTool(外部 MCP 工具 → Tool)、A2AAgentAdapter(远端 A2A agent → Agent)构成完整
的桥接三角:本地 agent → Tool。

run(arg) 即对子 agent 跑一步、取其输出包成带 provenance 的 ToolResult(tool = 工具名,默认取
子 agent 名,可溯源到产出它的子 agent)。最薄桥接:只搬运文本(子 agent 的 usage / error 不
透传);子 agent 抛异常照常上抛,与其它 Tool 一致——错误处理归编排层 / 调用方(见 Coordinator
弹性容错)。Tool 协议的 run 无 trace 形参,故 AgentTool 自身不发 trace,外层 agent 循环会为这次
工具调用发一条隐私安全的 tool_step(见 agent/tool_using.py)。
"""

from __future__ import annotations

from agentspine.agent.agent import Agent
from agentspine.tools.tool import ToolResult


class AgentTool:
    """跨原语适配器:把一个 Agent 桥成 Tool(实现 Tool 协议),用于分层 / 督导式多 agent。"""

    def __init__(self, agent: Agent, *, name: str | None = None) -> None:
        self._agent = agent
        self.name = name or agent.name

    def run(self, arg: str) -> ToolResult:
        result = self._agent.step(arg)
        return ToolResult(tool=self.name, output=result.output)
