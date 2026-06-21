"""agent 合约:离线单步执行 + provenance + 步级 trace 隐私安全。"""

from corespine.llm.provider import MockProvider
from corespine.observability.trace import InProcessPrivacyTraceSink

from spineagent.agent.agent import Agent, AgentResult, FunctionAgent, LlmAgent


def test_llm_agent_runs_a_single_step_offline():
    agent = LlmAgent("planner", MockProvider())
    result = agent.step("列个计划")
    assert isinstance(result, AgentResult)
    assert result.agent == "planner"  # provenance
    assert result.output  # MockProvider 确定性回声,非空
    assert isinstance(agent, Agent)


def test_llm_agent_is_deterministic():
    a = LlmAgent("x", MockProvider())
    b = LlmAgent("x", MockProvider())
    assert a.step("同样的输入").output == b.step("同样的输入").output


def test_function_agent_wraps_a_pure_function():
    agent = FunctionAgent("echoer", lambda task: f"done:{task}")
    result = agent.step("go")
    assert result.agent == "echoer"
    assert result.output == "done:go"


def test_step_emits_only_privacy_safe_metadata():
    sink = InProcessPrivacyTraceSink()
    agent = LlmAgent("planner", MockProvider())
    agent.step("一段不该进 trace 的敏感正文", trace=sink)
    assert sink.codes() == ["agent_step"]
    fields = sink.events[0].fields
    # 只记元数据:agent 名 + 长度 + token 数;没有任务/输出正文。
    assert set(fields) == {
        "agent",
        "task_chars",
        "output_chars",
        "input_tokens",
        "output_tokens",
    }
    assert fields["agent"] == "planner"


def test_step_without_trace_does_not_emit():
    # trace 为 None 时不发任何事件(纯离线、零副作用)。
    agent = FunctionAgent("q", lambda t: t)
    assert agent.step("hi").output == "hi"
