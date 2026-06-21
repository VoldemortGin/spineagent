"""会用工具的 agent 合约:多步循环 + $prev 链式 + max_steps 守卫 + 隐私 trace + 可编排。"""

import pytest
from corespine.observability.trace import InProcessPrivacyTraceSink

from spineagent.agent.agent import Agent
from spineagent.agent.policy import Action, Observation, SyntaxToolPolicy
from spineagent.agent.tool_using import ToolUsingAgent
from spineagent.orchestration.coordinator import Coordinator
from spineagent.tools.tool import CalcTool


def _calc_agent(name: str = "tu", *, max_steps: int = 8) -> ToolUsingAgent:
    return ToolUsingAgent(name, SyntaxToolPolicy(), [CalcTool()], max_steps=max_steps)


def test_single_tool_step_then_finish():
    result = _calc_agent().step("calc: 2+3")
    assert result.agent == "tu"  # provenance
    assert result.output  # 非空
    assert "5" in result.output


def test_multi_step_chains_prev_observation():
    # 第二步 $prev 替换为上一步输出('5'),calc('5 * 2') -> '10'。
    result = _calc_agent().step("calc: 2+3\ncalc: $prev * 2")
    assert "10" in result.output


def test_prev_on_first_step_substitutes_empty_string():
    # 首步 history 为空,$prev -> 空串;余下 '+5' 仍是合法算术,正常执行。
    result = _calc_agent().step("calc: $prev+5")
    assert "5" in result.output


def test_first_step_prev_alone_lets_tool_error_bubble():
    # 文档化契约:首步纯 $prev -> 空串,工具无法处理时其异常照常上抛(错误处理不在本增量范围)。
    with pytest.raises(SyntaxError):
        _calc_agent().step("calc: $prev")


def test_max_steps_guard_forces_finish_nonempty():
    # 三条工具指令但 max_steps=1:只能调一次工具,触顶强制收尾,产出非空。
    agent = _calc_agent(max_steps=1)
    result = agent.step("calc: 1+1\ncalc: 2+2\ncalc: 3+3")
    assert result.output  # 非空(取最后一步观测)
    assert result.agent == "tu"


def test_max_steps_equal_to_instruction_count_finishes_normally():
    # off-by-one 回归:N 条指令 + max_steps=N 应【正常收尾】,绝不误报触顶、绝不丢正文。
    sink = InProcessPrivacyTraceSink()
    result = _calc_agent(max_steps=2).step("结果如下:\ncalc: 1+1\ncalc: 2+2", trace=sink)
    assert sink.codes() == ["tool_step", "tool_step", "agent_finish"]
    assert "agent_step_limit" not in sink.codes()
    # 走 policy 精心拼装的答案(含正文行 + 最后观测),而非兜底分支。
    assert "结果如下:" in result.output
    assert "4" in result.output


def test_max_steps_is_a_tool_call_budget():
    # max_steps 语义 = 最大工具调用数:max_steps=2 恰好允许 2 次调用,第 3 条指令触顶。
    sink = InProcessPrivacyTraceSink()
    result = _calc_agent(max_steps=2).step("calc: 1+1\ncalc: 2+2\ncalc: 3+3", trace=sink)
    assert sink.codes().count("tool_step") == 2
    assert "agent_step_limit" in sink.codes()
    assert result.output  # 非空(取第 2 步观测)


def test_step_emits_only_privacy_safe_metadata():
    sink = InProcessPrivacyTraceSink()
    # 含「敏感正文」的任务:正文绝不该进 trace。
    _calc_agent().step("calc: 2+3\n机密代号 42 绝不入 trace", trace=sink)
    assert sink.codes() == ["tool_step", "agent_finish"]
    tool_fields = sink.events[0].fields
    assert set(tool_fields) == {"agent", "step", "tool", "arg_chars", "output_chars"}
    finish_fields = sink.events[1].fields
    assert set(finish_fields) == {"agent", "steps", "answer_chars"}
    # trace 里绝不出现任务 / 输出正文。
    for event in sink.events:
        assert all("机密代号" not in str(v) for v in event.fields.values())


def test_step_limit_emits_limit_event():
    sink = InProcessPrivacyTraceSink()
    _calc_agent(max_steps=1).step("calc: 1+1\ncalc: 2+2", trace=sink)
    assert "agent_step_limit" in sink.codes()


def test_is_agent_and_runs_in_coordinator():
    agent = _calc_agent()
    assert isinstance(agent, Agent)
    coord = Coordinator([_calc_agent("x"), _calc_agent("y")])
    seq = coord.run_sequential("calc: 6/3")
    par = coord.run_parallel("calc: 6/3")
    assert [r.agent for r in seq] == ["x", "y"]
    assert [r.agent for r in par] == ["x", "y"]
    assert all("2" in r.output for r in seq)


def test_policy_decide_returns_action_union():
    # 形状自检:decide 的返回值确属 Action 联合(ToolCall | Finish)。
    action: Action = SyntaxToolPolicy().decide("calc: 1+1", tools=("calc",), history=())
    assert action is not None
    # history 推进后收尾。
    done = SyntaxToolPolicy().decide(
        "calc: 1+1", tools=("calc",), history=(Observation("calc", "1+1", "2"),)
    )
    assert done.answer  # Finish.answer 非空
