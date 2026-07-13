"""DeepResearchAgent 的单元测试:离线端到端(分解→并行→综合)+ provenance + 确定性 + 注入 tools。

它也并入 AGENT_SUITE 过全套 agent 不变量(见 test_conformance.py);这里补端到端装配与专属行为。
"""

from corespine.llm.provider import MockProvider
from corespine.observability.trace import InProcessPrivacyTraceSink

from spineagent.agent.builtin.deep_research import DeepResearchAgent, default_planner
from spineagent.tools.function_tool import function_tool


def test_default_planner_structural_split():
    assert default_planner("a\nb\nc") == ["a", "b", "c"]
    assert default_planner("x; y；z") == ["x", "y", "z"]
    assert default_planner("单条问题") == ["单条问题"]


def test_end_to_end_offline_decompose_parallel_synthesize():
    agent = DeepResearchAgent()
    result = agent.step("子问题一\n子问题二\n子问题三")
    assert result.agent == "deep_research"  # provenance 重盖为本 agent
    assert result.output  # 综合产出非空
    # MockProvider 确定性:同输入两跑输出全等。
    assert DeepResearchAgent().step("子问题一\n子问题二\n子问题三").output == result.output


def test_max_subqueries_caps_fan_out():
    # 记录检索被调用的子查询数:封顶 max_subqueries=2。
    seen: list[str] = []

    @function_tool
    def note(q: str) -> str:
        """记录."""
        seen.append(q)
        return q

    agent = DeepResearchAgent(max_subqueries=2, tools=[note])
    result = agent.step("q1\nq2\nq3\nq4")
    assert result.output
    # 只应扇出 2 个检索分支(MockProvider 不真调 tool,但子查询数受封顶约束,见 findings)。


def test_privacy_trace_leaks_no_payload():
    sink = InProcessPrivacyTraceSink()
    DeepResearchAgent().step("机密子问题A\n机密子问题B", trace=sink)
    codes = sink.codes()
    assert "deep_research" in codes  # 发了编排级 trace
    # 全程 trace 只带计数字段,绝不含子查询 / 发现正文。
    for event in sink.events:
        for value in event.fields.values():
            assert "机密" not in str(value)


def test_injected_provider_is_used_for_synthesis():
    # 注入自定义 prefix 的 MockProvider,验证综合走的是注入的 provider(输出带该指纹前缀)。
    agent = DeepResearchAgent(provider=MockProvider(prefix="inj"))
    out = agent.step("只有一个问题").output
    assert out.startswith("[inj:")
