"""conformance 合约:用 corespine harness 把本包的不变量绑成参数化套件 + 检出泄露违反。

机制由 corespine.ConformanceSuite 提供(实现 × 不变量 笛卡尔积);保证由 spineagent 绑定
(ADR 0001 D6)。这里把三类实现各喂进自己的不变量包:
  - 6 个 agent 实现(llm / function / tool_using / a2a_adapter / chain / function_calling)× 3 条 agent 不变量;
  - 4 个 tool 实现(echo / calc / mcp_client_tool / agent_tool)× 2 条 tool 不变量;
  - 1 个 policy 实现(syntax)× 4 条 tool-policy 不变量。
跨原语适配器(McpClientTool / A2AAgentAdapter / AgentTool)与 ToolUsingAgent 都【复用既有不变量
包】跑全套——元模式红利:它们号称是 Tool / Agent,就必须过 Tool / Agent 的全部保证。
再用一个故意把任务正文写进 trace 的「泄露 agent」证明:隐私不变量格子会被 run() 标红。
"""

from types import SimpleNamespace

import pytest
from corespine.conformance.harness import ConformanceSuite
from corespine.llm.provider import MockProvider

from spineagent.agent.agent import AgentResult, FunctionAgent, LlmAgent
from spineagent.agent.as_tool import AgentTool
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.agent.policy import SyntaxToolPolicy
from spineagent.agent.tool_using import ToolUsingAgent
from spineagent.conformance import (
    AGENT_INVARIANTS,
    LLM_INVARIANTS,
    POLICY_INVARIANTS,
    SANDBOX_INVARIANTS,
    SKILL_INVARIANTS,
    TOOL_INVARIANTS,
)
from spineagent.llm.bedrock_provider import BedrockConverseProvider
from spineagent.llm.cohere_provider import CohereProvider
from spineagent.llm.gemini_provider import GeminiProvider
from spineagent.llm.provider import AnthropicProvider, OpenAICompatProvider
from spineagent.orchestration.chain import ChainAgent
from spineagent.protocol.a2a.seam import A2AAgentAdapter, OfflineA2AStub
from spineagent.protocol.mcp.seam import McpClientTool, McpTool, OfflineMcpStub
from spineagent.sandbox.seam import InProcessSandbox
from spineagent.skills.skill import FixtureSkill, SkillSpec
from spineagent.tools.tool import CalcTool, EchoTool


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
        "function_calling": lambda: FunctionCallingAgent("fc", MockProvider(), []),
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

# Sandbox conformance:离线套件只接离线默认 InProcessSandbox(真实硬隔离后端 subprocess / container
# 是 SeamError 桩,无法零参构造、也不该在零网络 CI 里跑——与 LLM_SUITE 只接可离线构造实现同理)。
SANDBOX_SUITE = ConformanceSuite({"in_process": InProcessSandbox}, SANDBOX_INVARIANTS)

# Skill conformance:离线默认 fixture skill(manifest + 受限表达式脚本,经 InProcessSandbox 执行)。
_SKILL_SPEC = SkillSpec(
    name="add",
    description="两个整数相加",
    inputs={
        "type": "object",
        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
        "required": ["x", "y"],
    },
)
SKILL_SUITE = ConformanceSuite(
    {"fixture": lambda: FixtureSkill(_SKILL_SPEC, "x + y")}, SKILL_INVARIANTS
)


# ---- LLMProvider conformance:MockProvider + 5 个真实后端(各注入 fake client,零真实 API)----
# 每个 fake client 在【无 tools】时回文本、【有 tools】时回一条形状完整的 tool_call(arguments 合法
# JSON),据此 LLM_INVARIANTS 的形状 / 取值域 / 非负 / 往返四条都能在离线、零网络下被验证。


class _ConfFakeAnthropic:
    """伪 anthropic 客户端:无 tools 回 text block + end_turn;有 tools 回 tool_use + tool_use stop。"""

    def __init__(self) -> None:
        self.messages = self

    def create(self, *, model, max_tokens, system, messages, tools=None, **extra):
        if tools:
            content = [SimpleNamespace(type="tool_use", id="tu1", name="calc", input={"x": 1})]
            stop = "tool_use"
        else:
            content = [SimpleNamespace(type="text", text="ok")]
            stop = "end_turn"
        return SimpleNamespace(
            content=content,
            stop_reason=stop,
            model="claude-x",
            id="msg_1",
            usage=SimpleNamespace(input_tokens=3, output_tokens=7),
        )


class _ConfFakeOpenAI:
    """伪 openai 客户端:无 tools 回 content + stop;有 tools 回 tool_calls + tool_calls finish。"""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self)

    def create(self, *, model, messages, max_tokens, tools=None, **extra):
        if tools:
            tc = SimpleNamespace(
                id="tc1", function=SimpleNamespace(name="calc", arguments='{"x": 1}')
            )
            message = SimpleNamespace(role="assistant", content=None, tool_calls=[tc])
            finish = "tool_calls"
        else:
            message = SimpleNamespace(role="assistant", content="ok", tool_calls=None)
            finish = "stop"
        return SimpleNamespace(
            choices=[SimpleNamespace(index=0, message=message, finish_reason=finish)],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=9, total_tokens=11),
            model=model,
            id="cmpl_1",
            created=0,
            object="chat.completion",
        )


class _ConfFakeCohere:
    """伪 cohere ClientV2:无 tools 回 text block + COMPLETE;有 tools 回 tool_calls + TOOL_CALL。"""

    def chat(self, *, model, messages, tools=None, **extra):
        if tools:
            tc = SimpleNamespace(
                id="c1",
                type="function",
                function=SimpleNamespace(name="calc", arguments='{"x": 1}'),
            )
            message = SimpleNamespace(content=[], tool_calls=[tc])
            finish = "TOOL_CALL"
        else:
            message = SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")], tool_calls=None
            )
            finish = "COMPLETE"
        usage = SimpleNamespace(tokens=SimpleNamespace(input_tokens=4, output_tokens=6))
        return SimpleNamespace(message=message, finish_reason=finish, usage=usage)


class _ConfFakeGemini:
    """伪 google-genai Client:无 tools 回 text part + STOP;有 tools 回 function_call part。"""

    def __init__(self) -> None:
        self.models = self

    def generate_content(self, *, model, contents, config=None):
        tools = (config or {}).get("tools")
        if tools:
            parts = [
                SimpleNamespace(
                    text=None, function_call=SimpleNamespace(name="calc", args={"x": 1})
                )
            ]
        else:
            parts = [SimpleNamespace(text="ok", function_call=None)]
        candidate = SimpleNamespace(
            content=SimpleNamespace(parts=parts), finish_reason=SimpleNamespace(name="STOP")
        )
        meta = SimpleNamespace(prompt_token_count=5, candidates_token_count=8, total_token_count=13)
        return SimpleNamespace(candidates=[candidate], usage_metadata=meta)


class _ConfFakeBedrock:
    """伪 boto3 bedrock-runtime:无 tools 回 text block + end_turn;有 tools 回 toolUse + tool_use。"""

    def converse(self, *, modelId, messages, **kwargs):
        if "toolConfig" in kwargs:
            content = [{"toolUse": {"toolUseId": "b1", "name": "calc", "input": {"x": 1}}}]
            stop = "tool_use"
        else:
            content = [{"text": "ok"}]
            stop = "end_turn"
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop,
            "usage": {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8},
        }


# 零参工厂(harness 要求):各注入 fake client，绝不触发真实 SDK / 网络 / API key。
LLM_SUITE = ConformanceSuite(
    {
        "mock": MockProvider,
        "anthropic": lambda: AnthropicProvider(client=_ConfFakeAnthropic()),
        "openai": lambda: OpenAICompatProvider("gpt-x", client=_ConfFakeOpenAI()),
        "cohere": lambda: CohereProvider(client=_ConfFakeCohere()),
        "gemini": lambda: GeminiProvider(client=_ConfFakeGemini()),
        "bedrock": lambda: BedrockConverseProvider("m", client=_ConfFakeBedrock()),
    },
    LLM_INVARIANTS,
)


@pytest.mark.parametrize(**AGENT_SUITE.parametrize_kwargs())
def test_agent_conformance(case):
    """每个 agent 实现 × 每条 agent 不变量 各跑一格(6 × 3 = 18 格全绿)。"""
    case()


@pytest.mark.parametrize(**TOOL_SUITE.parametrize_kwargs())
def test_tool_conformance(case):
    """每个 tool 实现 × 每条 tool 不变量 各跑一格(4 × 2 = 8 格全绿)。"""
    case()


@pytest.mark.parametrize(**POLICY_SUITE.parametrize_kwargs())
def test_policy_conformance(case):
    """每个 policy 实现 × 每条 tool-policy 不变量 各跑一格(1 × 4 = 4 格全绿)。"""
    case()


@pytest.mark.parametrize(**SANDBOX_SUITE.parametrize_kwargs())
def test_sandbox_conformance(case):
    """每个 Sandbox 实现(离线默认 in_process)× 每条 sandbox 不变量 各跑一格(1 × 5 = 5 格全绿)。"""
    case()


@pytest.mark.parametrize(**SKILL_SUITE.parametrize_kwargs())
def test_skill_conformance(case):
    """每个 skill 实现(离线默认 fixture)× 每条 skill 不变量 各跑一格(1 × 3 = 3 格全绿)。"""
    case()


@pytest.mark.parametrize(**LLM_SUITE.parametrize_kwargs())
def test_llm_provider_conformance(case):
    """每个 LLMProvider(mock + 5 后端,各注入 fake client)× 每条 llm 不变量 各跑一格
    (6 × 4 = 24 格全绿)。零真实 API:全部经 fake client 离线驱动。"""
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
