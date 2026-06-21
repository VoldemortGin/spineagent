"""spineagent 自己的不变量(机制借 corespine.conformance,保证由本包绑定,ADR 0001 D6)。

corespine 的 ConformanceSuite 只提供「实现 × 不变量」笛卡尔积的【机制】;具体保证在此绑定。
本包绑三组:

  agent_step  —— ①步必产出非空文本;②结果可溯源到产出它的 agent(provenance);
                 ③步级 trace 只记元数据,绝不泄露任务/输出正文(隐私安全,由 corespine 的
                   InProcessPrivacyTraceSink「构造即保证」兜底)。
  tool_call   —— ①工具结果可溯源到产出它的工具(provenance);②调用必产出非空文本。
  tool_policy —— 【实现中立】①决策是 ToolCall / Finish 之一(协议形状);②凡返回 ToolCall,
                 工具名必在可用集内(绝不幻觉一个不存在的工具);③可用工具为空时必返回 Finish
                 且答案非空(循环可终止 + 产出非空);④decide 是纯函数(同输入恒同输出)。
                 不预设「任务文本如何被解读为工具调用」——那是各实现的事,其专属断言归各实现
                 的单元测试(见 tests/test_policy.py)。

任何号称 Agent / Tool / ToolPolicy 的实现都必须跑过对应那组——没过 conformance 的实现直接红,
而非埋雷。
"""

from __future__ import annotations

from corespine.conformance.harness import InvariantPack
from corespine.observability.trace import FORBIDDEN_KEYS, InProcessPrivacyTraceSink

from spineagent.agent.agent import Agent, AgentResult
from spineagent.agent.policy import Finish, Observation, ToolCall, ToolPolicy
from spineagent.tools.tool import Tool

# 一段含敏感正文的任务:agent 若把它写进 trace 即泄露——隐私不变量要挡住的正是这个。
# 内嵌一个独特哨兵串(绝不会作为计数 / 长度 / agent 名巧合出现),用于「按值」检出泄露。
_SENSITIVE_MARKER = "绝密哨兵正文SENTINEL绝不入trace"
_SENSITIVE_TASK = f"机密档案:{_SENSITIVE_MARKER},严禁原样写入 trace。"


def _step_returns_output(agent: Agent) -> None:
    result = agent.step("ping")
    assert isinstance(result, AgentResult)
    assert result.output, "agent 步必须产出非空文本"


def _result_carries_agent_provenance(agent: Agent) -> None:
    result = agent.step("ping")
    assert result.agent == agent.name, "结果必须可溯源到产出它的 agent"


def _step_traces_are_privacy_safe(agent: Agent) -> None:
    sink = InProcessPrivacyTraceSink()
    # 隐私 by construction:agent 若试图把任务/输出正文写进 trace,emit 立刻抛 TraceError。
    agent.step(_SENSITIVE_TASK, trace=sink)
    assert sink.codes(), "agent 步至少应发一条元数据 trace"
    for event in sink.events:
        # ①按键名:命中 corespine FORBIDDEN_KEYS 的受限字段名(answer/value/text/...)。
        leaked = {k for k in event.fields if k.strip().lower() in FORBIDDEN_KEYS}
        assert not leaked, f"步级 trace 泄露了受限字段:{sorted(leaked)}"
        # ②按取值:即便键名不在禁词表,也绝不允许把敏感正文塞进任何字段值
        #   (corespine 的 sink 只查键名;本包再补一道「按值」防线,不只依赖弱守卫)。
        for key, value in event.fields.items():
            assert _SENSITIVE_MARKER not in str(value), f"步级 trace 字段 {key!r} 泄露了任务正文"


AGENT_INVARIANTS: InvariantPack[Agent] = (
    InvariantPack("agent_step")
    .add("step_returns_output", _step_returns_output)
    .add("result_carries_agent_provenance", _result_carries_agent_provenance)
    .add("step_traces_are_privacy_safe", _step_traces_are_privacy_safe)
)


def _tool_result_carries_provenance(tool: Tool) -> None:
    result = tool.run("1+1")
    assert result.tool == tool.name, "工具结果必须可溯源到产出它的工具"


def _tool_run_returns_output(tool: Tool) -> None:
    result = tool.run("1+1")
    assert result.output, "工具调用必须产出非空文本"


TOOL_INVARIANTS: InvariantPack[Tool] = (
    InvariantPack("tool_call")
    .add("result_carries_tool_provenance", _tool_result_carries_provenance)
    .add("run_returns_output", _tool_run_returns_output)
)


# tool-policy 不变量在检查函数内部自构造 task/tools/history(harness 工厂只给一个 policy 实例)。
# 【实现中立】:这些不变量只验任何 ToolPolicy 都该守的安全 / 终止 / 纯度属性,绝不预设「任务
# 文本如何被解读为工具调用」——那是各实现自己的事(如 SyntaxToolPolicy 的 `<tool>: <arg>` 语法、
# 未来 llm policy 的 function-calling),其专属断言归各实现的单元测试(见 tests/test_policy.py)。
_POLICY_TOOLS: tuple[str, ...] = ("calc",)


def _action_is_a_known_variant(policy: ToolPolicy) -> None:
    action = policy.decide("calc: 1+1", tools=_POLICY_TOOLS, history=())
    assert isinstance(action, (ToolCall, Finish)), "决策必须是 ToolCall / Finish 之一"


def _never_calls_an_unavailable_tool(policy: ToolPolicy) -> None:
    # 安全核心(不幻觉工具):无论任务怎么写,凡返回 ToolCall,其工具必在可用集内。
    # 用「点名一个不存在的工具」「点名一个存在的工具」「纯正文」三类任务一起施压。
    for task in ("ghost: x", "calc: 1+1", "纯文本无指令"):
        action = policy.decide(task, tools=_POLICY_TOOLS, history=())
        if isinstance(action, ToolCall):
            assert action.tool in _POLICY_TOOLS, f"绝不调用不在可用集内的工具:{action.tool!r}"


def _empty_tools_yields_nonempty_finish(policy: ToolPolicy) -> None:
    # 可用工具为空时,无工具可调 -> 必须收尾且答案非空(保证循环可终止 + 产出非空)。
    # 这条也能抓住「无视任务、永远调 tools[0]」的实现:空集下它要么越界、要么返回非 Finish。
    action = policy.decide("任意任务文本", tools=(), history=())
    assert isinstance(action, Finish), "可用工具为空时必须收尾,不得返回 ToolCall"
    assert action.answer, "收尾答案必须非空"


def _decide_is_pure(policy: ToolPolicy) -> None:
    history = (Observation(tool="calc", arg="1+1", output="2"),)
    first = policy.decide("calc: 1+1", tools=_POLICY_TOOLS, history=history)
    second = policy.decide("calc: 1+1", tools=_POLICY_TOOLS, history=history)
    assert first == second, "decide 必须是纯函数:同一 (task, tools, history) 恒定同一 Action"


POLICY_INVARIANTS: InvariantPack[ToolPolicy] = (
    InvariantPack("tool_policy")
    .add("action_is_a_known_variant", _action_is_a_known_variant)
    .add("never_calls_an_unavailable_tool", _never_calls_an_unavailable_tool)
    .add("empty_tools_yields_nonempty_finish", _empty_tools_yields_nonempty_finish)
    .add("decide_is_pure", _decide_is_pure)
)
