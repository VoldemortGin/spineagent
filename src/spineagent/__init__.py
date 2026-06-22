"""spineagent —— 通用多 agent 协作框架(ADR 0001 D1),依赖薄核 corespine。

agent / tool / 编排 + MCP / A2A 协议缝。复用 corespine 的缝元模式(Protocol + 离线确定性
默认 + Registry 工厂 + 参数化 conformance)、隐私安全 observability 与 env 配置风格;核心
默认路径【零网络、零重依赖、离线可跑】,真实协议 SDK 仅经可选 extra 延迟 import。

运行时可把 ragspine 当作一个 Tool / MCP server 在【运行时】组合调用(ADR 0001 D4b),但本包
【不】在包层面依赖 ragspine。详见 CLAUDE.md 宪章与家族 ADR 0001。
"""

# 运行时类型契约(对齐家族标准):装了 beartype 就在【调用期】对整个包强制每条注解,
# 与 mypy --strict(静态半边)互补。守护式 import:未装时离线核心照常 import。
# 务必放在【任何第一方 spineagent.* import 之前】,使 claw 钩子覆盖全包子模块。
try:
    from beartype import BeartypeConf
    from beartype.claw import beartype_this_package
except ImportError:  # 未装 beartype 时跳过运行时契约
    pass
else:
    # is_pep484_tower=True:采用 PEP 484 隐式数值塔(float 注解亦接受 int),与 mypy /
    # Python 约定一致——否则 beartype 会把 int-传-float 这类合法调用误判为违规。
    beartype_this_package(conf=BeartypeConf(is_pep484_tower=True))

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

# 版本号动态取自已安装包元数据(单一真相源 = pyproject.toml 的 version),杜绝再与硬编码
# 字符串漂移;未安装(纯源码场景)取不到时回退到一个常量。
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("spineagent")
except PackageNotFoundError:  # 未安装(直接从源码树 import)时回退
    __version__ = "0.0.0+unknown"

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
