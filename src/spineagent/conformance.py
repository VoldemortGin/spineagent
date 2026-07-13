"""spineagent 自己的不变量(机制借 corespine.conformance,保证由本包绑定,ADR 0001 D6)。

corespine 的 ConformanceSuite 只提供「实现 × 不变量」笛卡尔积的【机制】;具体保证在此绑定。
本包绑四组:

  agent_step  —— ①步必产出非空文本;②结果可溯源到产出它的 agent(provenance);
                 ③步级 trace 只记元数据,绝不泄露任务/输出正文(隐私安全,由 corespine 的
                   InProcessPrivacyTraceSink「构造即保证」兜底)。
  tool_call   —— ①工具结果可溯源到产出它的工具(provenance);②调用必产出非空文本。
  tool_policy —— 【实现中立】①决策是 ToolCall / Finish 之一(协议形状);②凡返回 ToolCall,
                 工具名必在可用集内(绝不幻觉一个不存在的工具);③可用工具为空时必返回 Finish
                 且答案非空(循环可终止 + 产出非空);④decide 是纯函数(同输入恒同输出)。
                 不预设「任务文本如何被解读为工具调用」——那是各实现的事,其专属断言归各实现
                 的单元测试(见 tests/test_policy.py)。
  llm_provider —— 【对外唯一规范 = OpenAI chat completions 形状】任何 LLMProvider 适配器(无论
                 底层是 Anthropic / OpenAI / Cohere / Gemini / Bedrock)都必须把响应规整成同一份
                 OpenAI ChatCompletion 形状:①chat() 返回 ChatCompletion、choices 非空、每个 choice
                 带 message 与 finish_reason;②finish_reason 落在合法取值域(stop / tool_calls /
                 length / content_filter);③usage 存在时三个 token 字段非负;④给了 tools 且模型
                 发了 tool_calls 时,每条 tool_call 形状可往返(id / function.name 非空、
                 function.arguments 是合法 JSON)。绝不预设具体文本/工具名——那是各适配器单测的事。
  sandbox     —— 【隔离契约,实现中立】①结果可溯源到产出它的沙箱(provenance);②执行必产出非空
                 文本;③资源记账非负(ops / output_chars / wall_seconds);④资源上限生效(给 0
                 字符产出上限必判失败);⑤无网络出口(网络探针必被拒绝 / 判失败)。
  skill       —— ①describe() 返回确定性 schema(同一 skill 恒定同一);②invoke 结果可溯源到产出
                 它的 skill(provenance)且产出非空。
  middleware  —— ①before_step 返回 None(形状);②after_step 返回 AgentResult 且保留结果 provenance;
                 ③包裹后单步确定性(同输入两跑,输出 + trace code 序列全等);④包裹后步级 trace 零
                 正文泄漏(隐私安全)。

任何号称 Agent / Tool / ToolPolicy / LLMProvider / Sandbox / Skill / Middleware 的实现都必须跑过
对应那组——没过 conformance 的实现直接红,而非埋雷。
"""

import json

from corespine.conformance.harness import InvariantPack
from corespine.llm.provider import ChatCompletion, LLMProvider
from corespine.observability.trace import FORBIDDEN_KEYS, InProcessPrivacyTraceSink

from spineagent.agent.agent import Agent, AgentResult, FunctionAgent
from spineagent.agent.middleware import Middleware, MiddlewareAgent, StepContext
from spineagent.agent.policy import Finish, Observation, ToolCall, ToolPolicy
from spineagent.sandbox.seam import Limits, Sandbox
from spineagent.skills.skill import Skill, SkillResult
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


# ---- LLMProvider 不变量(对外唯一规范 = OpenAI ChatCompletion 形状)-----------------------
# 不变量检查函数内部自构造 messages / tools(harness 工厂只给一个 provider 实例)。【实现中立】:
# 只验任何 LLMProvider 都该守的「形状 / 取值域 / 非负 / 往返」属性,绝不预设具体文本或工具名——
# 那是各适配器单测的事(见 tests/test_llm_provider.py / test_native_providers.py)。
_FINISH_REASONS: frozenset[str] = frozenset({"stop", "tool_calls", "length", "content_filter"})
_LLM_MESSAGES: list[dict[str, object]] = [{"role": "user", "content": "ping"}]
_LLM_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "calc",
        "description": "算术",
        "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
    },
}


def _chat_returns_chatcompletion_shape(provider: LLMProvider) -> None:
    result = provider.chat(_LLM_MESSAGES)
    assert isinstance(result, ChatCompletion), "chat() 必须返回 ChatCompletion"
    assert result.choices, "ChatCompletion.choices 必须非空"
    for choice in result.choices:
        assert choice.message is not None, "每个 choice 必须带 message"
        assert isinstance(choice.finish_reason, str), "每个 choice 必须带 finish_reason"


def _finish_reason_in_allowed_domain(provider: LLMProvider) -> None:
    # 不带 tools(可能出文本)与带 tools(可能出 tool_calls)两路都施压,覆盖更多 finish_reason 分支。
    for tools in (None, [_LLM_TOOL]):
        result = provider.chat(_LLM_MESSAGES, tools=tools)
        for choice in result.choices:
            assert choice.finish_reason in _FINISH_REASONS, (
                f"finish_reason 越出合法取值域:{choice.finish_reason!r}"
            )


def _usage_fields_are_non_negative(provider: LLMProvider) -> None:
    usage = provider.chat(_LLM_MESSAGES).usage
    if usage is None:  # usage 可空(部分端点精简/流式),空则跳过(非负只约束存在时)
        return
    assert usage.prompt_tokens >= 0, "usage.prompt_tokens 不得为负"
    assert usage.completion_tokens >= 0, "usage.completion_tokens 不得为负"
    assert usage.total_tokens >= 0, "usage.total_tokens 不得为负"


def _tool_calls_round_trip(provider: LLMProvider) -> None:
    # 给了 tools 时,若模型发了 tool_calls,每条都必须形状完整且 arguments 是合法 JSON
    # (能被下一轮原样喂回 = 往返)。模型【不】发 tool_calls 也合法(如离线 MockProvider),跳过即可。
    result = provider.chat(_LLM_MESSAGES, tools=[_LLM_TOOL])
    for choice in result.choices:
        for call in choice.message.tool_calls or ():
            assert call.id, "tool_call.id 必须非空"
            assert call.function.name, "tool_call.function.name 必须非空"
            json.loads(call.function.arguments)  # 非法 JSON 在此抛 → 不变量红


LLM_INVARIANTS: InvariantPack[LLMProvider] = (
    InvariantPack("llm_provider")
    .add("chat_returns_chatcompletion_shape", _chat_returns_chatcompletion_shape)
    .add("finish_reason_in_allowed_domain", _finish_reason_in_allowed_domain)
    .add("usage_fields_are_non_negative", _usage_fields_are_non_negative)
    .add("tool_calls_round_trip", _tool_calls_round_trip)
)


# ---- Sandbox 不变量(隔离契约)------------------------------------------------------------------
# 【实现中立】只验任何 Sandbox 都该守的隔离 / 记账 / 溯源属性,用一段所有后端都必须能跑的规范代码
# (纯算术 "1 + 1")驱动,再用一段网络探针钉死「无网络出口」。绝不预设某后端如何解释 code——那是
# 各后端单测的事(见 tests/test_sandbox.py)。注:离线 conformance 套件只接【离线默认】InProcessSandbox
# (与 LLM_SUITE 只接可离线构造的实现同理);真实硬隔离后端过不过这几条,是其接入时自己的责任。
_SANDBOX_CANONICAL = "1 + 1"
# 一段试图开网络出口的探针:任何守约的沙箱都必须【拒绝执行 / 判失败】,绝不真的连出去。
_SANDBOX_NETWORK_PROBE = "__import__('socket').socket()"


def _result_carries_sandbox_provenance(sandbox: Sandbox) -> None:
    result = sandbox.run(_SANDBOX_CANONICAL)
    assert result.sandbox == sandbox.name, "结果必须可溯源到产出它的沙箱"


def _run_produces_output(sandbox: Sandbox) -> None:
    assert sandbox.run(_SANDBOX_CANONICAL).output, "沙箱执行必须产出非空文本"


def _usage_is_accounted(sandbox: Sandbox) -> None:
    usage = sandbox.run(_SANDBOX_CANONICAL).usage
    assert usage.ops >= 0, "usage.ops 不得为负"
    assert usage.output_chars >= 0, "usage.output_chars 不得为负"
    assert usage.wall_seconds >= 0.0, "usage.wall_seconds 不得为负"


def _resource_limit_takes_effect(sandbox: Sandbox) -> None:
    # 给一个「必然被超出」的产出上限(0 字符):守约沙箱必判失败,绝不放行完整产出。
    result = sandbox.run(_SANDBOX_CANONICAL, limits=Limits(max_output_chars=0))
    assert not result.ok, "资源上限必须生效:产出超上限时执行不得成功"


def _no_network_egress(sandbox: Sandbox) -> None:
    # 隔离核心:试图开网络出口的代码必须被拒绝 / 判失败(离线套件里由 InProcessSandbox 的 AST
    # 白名单构造即保证;真实后端须由 OS 级隔离封住,否则接入时这条会如实标红)。
    result = sandbox.run(_SANDBOX_NETWORK_PROBE)
    assert not result.ok, "沙箱绝不允许网络出口:网络探针必须被拒绝 / 判失败"


SANDBOX_INVARIANTS: InvariantPack[Sandbox] = (
    InvariantPack("sandbox")
    .add("result_carries_sandbox_provenance", _result_carries_sandbox_provenance)
    .add("run_produces_output", _run_produces_output)
    .add("usage_is_accounted", _usage_is_accounted)
    .add("resource_limit_takes_effect", _resource_limit_takes_effect)
    .add("no_network_egress", _no_network_egress)
)


# ---- Skill 不变量(能力包契约,实现中立)------------------------------------------------------
# 从 describe() 的 schema 合成一份「零值」样例参数驱动 invoke——故不预设某 skill 的具体 inputs,
# 任何 skill(含第三方经 entry-point 装入的)都能被同一套不变量施压。skill 专属的语义(脚本算什么)
# 归各实现单测(见 tests/test_skills.py)。
_JSON_ZERO: dict[str, object] = {
    "string": "",
    "integer": 0,
    "number": 0,
    "boolean": False,
    "array": [],
    "object": {},
}


def _sample_args_from_schema(schema: dict[str, object]) -> dict[str, object]:
    """从 describe() 的 function schema 合成一份满足 required 的零值样例参数。"""
    params = schema.get("function", {})
    parameters = params.get("parameters", {}) if isinstance(params, dict) else {}
    properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    required = parameters.get("required", []) if isinstance(parameters, dict) else []
    args: dict[str, object] = {}
    if isinstance(properties, dict):
        names = required if isinstance(required, list) and required else list(properties)
        for name in names:
            spec = properties.get(name, {}) if isinstance(properties, dict) else {}
            json_type = spec.get("type") if isinstance(spec, dict) else None
            args[name] = _JSON_ZERO.get(json_type, 0) if isinstance(json_type, str) else 0
    return args


def _describe_returns_deterministic_schema(skill: Skill) -> None:
    first = skill.describe()
    second = skill.describe()
    assert isinstance(first, dict) and first, "describe() 必须返回非空 schema dict"
    assert first == second, "describe() 必须确定性:同一 skill 恒定同一 schema"


def _invoke_carries_skill_provenance(skill: Skill) -> None:
    result = skill.invoke(_sample_args_from_schema(skill.describe()))
    assert isinstance(result, SkillResult)
    assert result.skill == skill.spec.name, "结果必须可溯源到产出它的 skill"


def _invoke_returns_output(skill: Skill) -> None:
    result = skill.invoke(_sample_args_from_schema(skill.describe()))
    assert result.output, "skill 调用必须产出非空文本"


SKILL_INVARIANTS: InvariantPack[Skill] = (
    InvariantPack("skill")
    .add("describe_returns_deterministic_schema", _describe_returns_deterministic_schema)
    .add("invoke_carries_skill_provenance", _invoke_carries_skill_provenance)
    .add("invoke_returns_output", _invoke_returns_output)
)


# ---- Middleware 不变量(链包裹契约,实现中立)-------------------------------------------------
# 只验任何 Middleware 都该守的形状 / provenance 保全 / 包裹后确定性 / 包裹后 trace 零泄漏。把待测
# middleware 单元素成链包住一个 fixture FunctionAgent 施压——各 middleware 专属语义(压缩 / 记账 /
# 工具调度 / 附件)归各实现单测(见 tests/test_middleware.py)。
def _mw_wrap(mw: Middleware) -> MiddlewareAgent:
    return MiddlewareAgent("mw", FunctionAgent("inner", lambda task: f"done:{task}"), [mw])


def _before_step_returns_none(mw: Middleware) -> None:
    # before_step 由协议保证返回 None;此处只验它对一个全新 ctx 不抛(形状 / 健壮性)。
    mw.before_step(StepContext(agent="mw", task="ping"))


def _after_step_preserves_provenance(mw: Middleware) -> None:
    ctx = StepContext(agent="mw", task="ping")
    mw.before_step(ctx)  # 有的 middleware 在 before 里备好 after 要用的元数据
    result = AgentResult(agent="inner", output="ok")
    out = mw.after_step(ctx, result)
    assert isinstance(out, AgentResult), "after_step 必须返回 AgentResult"
    assert out.agent == result.agent, "after_step 必须保留结果 provenance,绝不吞掉来源"


def _wrapped_step_is_deterministic(mw: Middleware) -> None:
    # 同一 (中间件, 输入) 两跑:输出 + trace code 序列必须全等(顺序 / 决策确定性)。
    first_sink, second_sink = InProcessPrivacyTraceSink(), InProcessPrivacyTraceSink()
    first = _mw_wrap(mw).step("ping", trace=first_sink)
    second = _mw_wrap(mw).step("ping", trace=second_sink)
    assert first.output == second.output, "包裹后同输入必产同输出"
    assert first_sink.codes() == second_sink.codes(), "包裹后 trace code 序列必确定"


def _wrapped_trace_is_privacy_safe(mw: Middleware) -> None:
    sink = InProcessPrivacyTraceSink()
    _mw_wrap(mw).step(_SENSITIVE_TASK, trace=sink)
    assert sink.codes(), "包裹后至少应发一条元数据 trace"
    for event in sink.events:
        leaked = {k for k in event.fields if k.strip().lower() in FORBIDDEN_KEYS}
        assert not leaked, f"middleware trace 泄露了受限字段:{sorted(leaked)}"
        for key, value in event.fields.items():
            assert _SENSITIVE_MARKER not in str(value), f"middleware trace 字段 {key!r} 泄露了正文"


MIDDLEWARE_INVARIANTS: InvariantPack[Middleware] = (
    InvariantPack("middleware")
    .add("before_step_returns_none", _before_step_returns_none)
    .add("after_step_preserves_provenance", _after_step_preserves_provenance)
    .add("wrapped_step_is_deterministic", _wrapped_step_is_deterministic)
    .add("wrapped_trace_is_privacy_safe", _wrapped_trace_is_privacy_safe)
)
