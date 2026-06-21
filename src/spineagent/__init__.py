"""spineagent —— 通用多 agent 协作框架(ADR 0001 D1),依赖薄核 corespine。

agent / tool / 编排 + MCP / A2A 协议缝。复用 corespine 的缝元模式(Protocol + 离线确定性
默认 + Registry 工厂 + 参数化 conformance)、隐私安全 observability 与 env 配置风格;核心
默认路径【零网络、零重依赖、离线可跑】,真实协议 SDK 仅经可选 extra 延迟 import。

运行时可把 ragspine 当作一个 Tool / MCP server 在【运行时】组合调用(ADR 0001 D4b),但本包
【不】在包层面依赖 ragspine。详见 CLAUDE.md 宪章与家族 ADR 0001。
"""

from spineagent.agent.agent import Agent, AgentResult, FunctionAgent, LlmAgent
from spineagent.agent.as_tool import AgentTool
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.agent.policy import (
    Action,
    Finish,
    Observation,
    SyntaxToolPolicy,
    ToolCall,
    ToolPolicy,
    tool_policies,
)
from spineagent.agent.tool_using import ToolUsingAgent
from spineagent.conformance import AGENT_INVARIANTS, POLICY_INVARIANTS, TOOL_INVARIANTS
from spineagent.llm.bedrock_provider import BedrockConverseProvider, load_boto3_sdk
from spineagent.llm.cohere_provider import CohereProvider, load_cohere_sdk
from spineagent.llm.gemini_provider import GeminiProvider, load_gemini_sdk
from spineagent.llm.provider import (
    AnthropicProvider,
    OpenAICompatProvider,
    llm_providers,
    load_anthropic_sdk,
    load_openai_sdk,
)
from spineagent.orchestration.chain import ChainAgent
from spineagent.orchestration.coordinator import Coordinator
from spineagent.protocol.a2a.seam import (
    A2AAgent,
    A2AAgentAdapter,
    A2AResult,
    A2ATask,
    OfflineA2AStub,
    a2a_agents,
    load_a2a_sdk,
)
from spineagent.protocol.mcp.seam import (
    McpClient,
    McpClientTool,
    McpServer,
    McpTool,
    OfflineMcpStub,
    load_mcp_sdk,
    mcp_clients,
)
from spineagent.tools.function_tool import FunctionTool, function_tool
from spineagent.tools.tool import CalcTool, EchoTool, Tool, ToolResult, tool_registry

__version__ = "0.0.1"

__all__ = [
    # agent
    "Agent",
    "AgentResult",
    "LlmAgent",
    "FunctionAgent",
    "ToolUsingAgent",
    "AgentTool",
    "FunctionCallingAgent",
    # tool-policy 缝(会用工具的 agent 的「大脑」)
    "ToolPolicy",
    "ToolCall",
    "Finish",
    "Action",
    "Observation",
    "SyntaxToolPolicy",
    "tool_policies",
    # tools
    "Tool",
    "ToolResult",
    "EchoTool",
    "CalcTool",
    "tool_registry",
    "FunctionTool",
    "function_tool",
    # orchestration
    "Coordinator",
    "ChainAgent",
    # llm provider 适配器(挂在 corespine LLMProvider 缝后面;输出统一 OpenAI ChatCompletion)
    "AnthropicProvider",
    "OpenAICompatProvider",
    "CohereProvider",
    "GeminiProvider",
    "BedrockConverseProvider",
    "llm_providers",
    "load_anthropic_sdk",
    "load_openai_sdk",
    "load_cohere_sdk",
    "load_gemini_sdk",
    "load_boto3_sdk",
    # protocol: mcp
    "McpClient",
    "McpServer",
    "McpTool",
    "McpClientTool",
    "OfflineMcpStub",
    "mcp_clients",
    "load_mcp_sdk",
    # protocol: a2a
    "A2AAgent",
    "A2ATask",
    "A2AResult",
    "A2AAgentAdapter",
    "OfflineA2AStub",
    "a2a_agents",
    "load_a2a_sdk",
    # conformance (本包绑定的不变量)
    "AGENT_INVARIANTS",
    "TOOL_INVARIANTS",
    "POLICY_INVARIANTS",
    "__version__",
]
