"""tool-policy 缝合约:SyntaxToolPolicy 确定性路由 + 语法解析 + Registry(offline/llm/未知)。"""

import pytest
from corespine.errors import SeamError

from agentspine.agent.policy import (
    Finish,
    Observation,
    SyntaxToolPolicy,
    ToolCall,
    ToolPolicy,
    tool_policies,
)


def test_offline_policy_satisfies_protocol():
    assert isinstance(SyntaxToolPolicy(), ToolPolicy)


def test_first_instruction_routes_to_named_tool():
    action = SyntaxToolPolicy().decide("calc: 2+3", tools=("calc",), history=())
    assert action == ToolCall(tool="calc", arg="2+3")


def test_cursor_advances_with_history_length():
    task = "calc: 2+3\ncalc: 10/2"
    history = (Observation(tool="calc", arg="2+3", output="5"),)
    # history 长度 1 -> 游标指向第 2 条工具指令。
    action = SyntaxToolPolicy().decide(task, tools=("calc",), history=history)
    assert action == ToolCall(tool="calc", arg="10/2")


def test_finishes_when_instructions_exhausted_keeping_prose():
    task = "calc: 2+3\n请汇总结果"
    history = (Observation(tool="calc", arg="2+3", output="5"),)
    action = SyntaxToolPolicy().decide(task, tools=("calc",), history=history)
    assert isinstance(action, Finish)
    # 收尾答案含非指令正文行 + 最后一步观测输出。
    assert "请汇总结果" in action.answer
    assert "5" in action.answer


def test_pure_text_task_finishes_immediately():
    action = SyntaxToolPolicy().decide("没有任何工具指令的纯文本", tools=("calc",), history=())
    assert isinstance(action, Finish)
    assert action.answer == "没有任何工具指令的纯文本"


def test_routes_to_the_named_tool_not_merely_the_first():
    # 多工具:任务点名第二个,必须路由到第二个(而非无脑调 tools[0])。
    action = SyntaxToolPolicy().decide("search: 北京天气", tools=("calc", "search"), history=())
    assert action == ToolCall(tool="search", arg="北京天气")


def test_unregistered_tool_name_is_treated_as_prose():
    # tools 里没有 foo:'foo: x' 不被当指令路由,而是当正文 -> 立即收尾(不幻觉一个不存在的工具)。
    action = SyntaxToolPolicy().decide("foo: x", tools=("calc",), history=())
    assert isinstance(action, Finish)
    assert "foo: x" in action.answer


def test_empty_tools_always_finishes():
    # 可用工具为空:任何行都不可能匹配 -> 立即收尾(实现中立不变量的具体落地)。
    action = SyntaxToolPolicy().decide("calc: 1+1", tools=(), history=())
    assert isinstance(action, Finish)
    assert action.answer


def test_decide_is_deterministic():
    policy = SyntaxToolPolicy()
    task = "calc: 1+1"
    a = policy.decide(task, tools=("calc",), history=())
    b = policy.decide(task, tools=("calc",), history=())
    assert a == b


def test_registry_makes_offline_default():
    policy = tool_policies.make("offline")
    assert isinstance(policy, ToolPolicy)
    assert "offline" in tool_policies.names()
    assert "llm" in tool_policies.names()


def test_registry_llm_placeholder_not_implemented():
    # 缝槽存在但真实实现未接入:家族统一 SeamError(code="seam.unknown")。
    with pytest.raises(SeamError) as ei:
        tool_policies.make("llm")
    assert ei.value.code == "seam.unknown"


def test_registry_unknown_spec_lists_available():
    with pytest.raises(ValueError) as ei:
        tool_policies.make("nope")
    msg = str(ei.value)
    assert "offline" in msg and "llm" in msg
