"""chain 合约:把一串 agent 串成单个 Agent + 可组合(进 Coordinator / 当工具 / chain 套 chain)。"""

import pytest
from corespine.observability.trace import InProcessPrivacyTraceSink

from spineagent.agent.agent import Agent, FunctionAgent
from spineagent.agent.as_tool import AgentTool
from spineagent.agent.policy import SyntaxToolPolicy
from spineagent.agent.tool_using import ToolUsingAgent
from spineagent.orchestration.chain import ChainAgent
from spineagent.orchestration.coordinator import Coordinator
from spineagent.tools.tool import CalcTool


def _abc_chain() -> ChainAgent:
    return ChainAgent(
        "etl",
        [
            FunctionAgent("a", lambda t: f"a:{t}"),
            FunctionAgent("b", lambda t: f"b:{t}"),
            FunctionAgent("c", lambda t: f"c:{t}"),
        ],
    )


def test_chain_threads_output_returns_final_with_chain_provenance():
    result = _abc_chain().step("go")
    # a→b→c 链式:c 吃到 "b:a:go" 产出 "c:b:a:go";provenance 归 chain 名。
    assert result.agent == "etl"
    assert result.output == "c:b:a:go"


def test_chain_is_an_agent():
    assert isinstance(_abc_chain(), Agent)


def test_empty_chain_is_identity_passthrough():
    result = ChainAgent("empty", []).step("go")
    assert result.agent == "empty"
    assert result.output == "go"


def test_chain_runs_inside_coordinator():
    coord = Coordinator([_abc_chain(), FunctionAgent("x", lambda t: f"x:{t}")])
    results = coord.run_sequential("go")
    assert [r.agent for r in results] == ["etl", "x"]
    assert results[0].output == "c:b:a:go"


def test_chain_as_a_tool_and_nested_chains():
    # chain 当工具:督导 agent 把任务派给一个 chain;chain 里再嵌一个会用工具的 agent。
    inner = ChainAgent("inner", [ToolUsingAgent("calc", SyntaxToolPolicy(), [CalcTool()])])
    supervisor = ToolUsingAgent("sup", SyntaxToolPolicy(), [AgentTool(inner, name="inner")])
    assert "5" in supervisor.step("inner: calc: 2+3").output


def test_chain_fail_fast_bubbles():
    def _boom(_t: str) -> str:
        raise ValueError("boom")

    chain = ChainAgent("c", [FunctionAgent("a", lambda t: t), FunctionAgent("boom", _boom)])
    with pytest.raises(ValueError):
        chain.step("go")


def test_chain_step_trace_is_privacy_safe():
    sink = InProcessPrivacyTraceSink()
    _abc_chain().step("机密正文 42 绝不入 trace", trace=sink)
    assert sink.codes() == ["chain_step"]
    fields = sink.events[0].fields
    assert set(fields) == {"agent", "stages", "output_chars"}
    assert fields["stages"] == 3
    for value in fields.values():
        assert "机密正文" not in str(value)
