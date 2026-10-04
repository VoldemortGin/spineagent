"""真 function-calling agent 合约:LLM 回 tool_calls → 执行工具 → 喂回 → 再 chat → 出文本。

用一个【脚本化的 fake LLMProvider】(按预设依次返回 ChatCompletion)离线驱动多轮工具调用循环,
绝不真连网络。验证:工具确被执行、结果以 OpenAI tool 角色喂回、最终出文本;以及 schema / 装饰器 /
max_steps / 可组合 / 隐私 trace。
"""

import pytest
from corespine.llm.provider import (
    ChatCompletion,
    Choice,
    FunctionCall,
    MockProvider,
    ResponseMessage,
    Usage,
)
from corespine.llm.provider import ToolCall as LLMToolCall
from corespine.observability.trace import InProcessPrivacyTraceSink

from spineagent.agent.agent import Agent
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.orchestration.coordinator import Coordinator
from spineagent.tools.function_tool import (
    FunctionTool,
    InvalidToolArguments,
    function_tool,
)


class _ScriptedProvider:
    """按脚本依次返回 ChatCompletion 的 fake provider;记录每次 chat 收到的 messages。"""

    def __init__(self, *responses: ChatCompletion) -> None:
        self._responses = list(responses)
        self._i = 0
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.calls.append([dict(m) for m in messages])
        resp = self._responses[self._i]
        self._i += 1
        return resp


def _text(content: str) -> ChatCompletion:
    return ChatCompletion(
        choices=(Choice(index=0, message=ResponseMessage(role="assistant", content=content)),),
        usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
    )


def _tool(call_id: str, name: str, arguments: str) -> ChatCompletion:
    msg = ResponseMessage(
        role="assistant",
        content=None,
        tool_calls=(
            LLMToolCall(id=call_id, function=FunctionCall(name=name, arguments=arguments)),
        ),
    )
    return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="tool_calls"),))


def _calc_tool(spy: list) -> FunctionTool:
    def calc(expression: str) -> str:
        """对一个算术表达式求值。"""
        spy.append(expression)
        from spineagent.tools.tool import CalcTool

        return CalcTool().run(expression).output

    return FunctionTool(
        name="calc",
        description="算术求值",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        func=calc,
    )


def test_function_calling_loop_executes_tool_then_answers():
    spy: list = []
    model = _ScriptedProvider(
        _tool("c1", "calc", '{"expression": "2+3"}'),  # 第 1 轮:模型要调 calc
        _text("结果是 5"),  # 第 2 轮:模型出最终文本
    )
    agent = FunctionCallingAgent("solver", model, [_calc_tool(spy)])
    result = agent.step("2+3 等于几?")
    assert result.agent == "solver"  # provenance
    assert result.output == "结果是 5"
    assert spy == ["2+3"]  # calc 确被执行
    # 第 2 次 chat 的对话里应含 assistant(tool_calls)与 tool 角色结果("5")
    second_turn = model.calls[1]
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in second_turn)
    tool_msg = next(m for m in second_turn if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == "c1" and tool_msg["content"] == "5"


def test_no_tool_calls_returns_text_directly():
    agent = FunctionCallingAgent("a", _ScriptedProvider(_text("直接回答")), [_calc_tool([])])
    assert agent.step("hi").output == "直接回答"


def test_unknown_tool_name_is_reported_not_crashed():
    spy: list = []
    model = _ScriptedProvider(_tool("c1", "ghost", "{}"), _text("已处理"))
    result = FunctionCallingAgent("a", model, [_calc_tool(spy)]).step("x")
    assert result.output == "已处理"
    tool_msg = next(m for m in model.calls[1] if m.get("role") == "tool")
    assert "unknown tool" in tool_msg["content"]  # 未知工具回错误消息而非崩溃


def test_max_steps_guard_forces_nonempty_finish():
    # 模型每轮都要工具、永不收尾:触顶后强制非空收尾。
    model = _ScriptedProvider(*[_tool(f"c{i}", "calc", '{"expression": "1+1"}') for i in range(5)])
    result = FunctionCallingAgent("a", model, [_calc_tool([])], max_steps=2).step("x")
    assert result.output  # 非空(兜底)


def test_offline_mock_provider_answers_without_tools():
    # 离线默认 MockProvider 不回 tool_calls,直接出文本(诚实:不假装会 function-calling)。
    agent = FunctionCallingAgent("a", MockProvider(), [_calc_tool([])])
    assert agent.step("ping").output  # 非空确定性文本


def test_is_agent_and_composes_in_coordinator():
    agent = FunctionCallingAgent("fc", _ScriptedProvider(_text("ok")), [])
    assert isinstance(agent, Agent)
    coord = Coordinator([agent, FunctionCallingAgent("fc2", _ScriptedProvider(_text("ok2")), [])])
    assert [r.output for r in coord.run_sequential("go")] == ["ok", "ok2"]


def test_step_trace_is_privacy_safe():
    echo = FunctionTool(
        "echo",
        "回显",
        {"type": "object", "properties": {"text": {"type": "string"}}},
        func=lambda text: text,
    )
    model = _ScriptedProvider(_tool("c1", "echo", '{"text": "机密 2+3"}'), _text("机密结果"))
    sink = InProcessPrivacyTraceSink()
    FunctionCallingAgent("s", model, [echo]).step("机密任务", trace=sink)
    assert sink.codes() == ["tool_step", "agent_finish"]
    for event in sink.events:
        assert set(event.fields) <= {
            "agent",
            "step",
            "tool",
            "arg_chars",
            "output_chars",
            "steps",
            "answer_chars",
        }
        assert all("机密" not in str(v) for v in event.fields.values())  # 按值不泄露


# ---- FunctionTool / 装饰器 -------------------------------------------------------------


def test_function_tool_schema_is_openai_shape():
    ft = FunctionTool(
        "calc",
        "算术",
        {"type": "object", "properties": {"x": {"type": "string"}}},
        func=lambda x: x,
    )
    s = ft.schema()
    assert s["type"] == "function"
    assert s["function"]["name"] == "calc"
    assert s["function"]["parameters"]["properties"]["x"]["type"] == "string"


def test_function_tool_invoke_calls_func_with_dict_args():
    ft = FunctionTool("add", "", {}, func=lambda a, b: a + b)
    assert ft.invoke({"a": 2, "b": 3}) == "5"  # 结果转字符串


def test_function_tool_invoke_bad_kwargs_raises_typeerror():
    # 特征化:invoke 把 args 当 **kwargs 解包,键不匹配底层函数签名 → TypeError(invoke 本身不吞;
    # 边界校验在 parse_arguments,见下方一组 parse_arguments 测试)。
    ft = FunctionTool("add", "", {}, func=lambda a, b: a + b)
    with pytest.raises(TypeError):
        ft.invoke({"a": 1, "wrong": 2})


# ---- M1:FunctionTool.parse_arguments 边界校验(用工具自带 schema)-------------------------


def _typed_tool() -> FunctionTool:
    return FunctionTool(
        "calc",
        "算术",
        {
            "type": "object",
            "properties": {"expression": {"type": "string"}, "n": {"type": "integer"}},
            "required": ["expression"],
        },
        func=lambda expression, n=1: f"{expression}*{n}",
    )


def test_parse_arguments_accepts_valid_payload():
    assert _typed_tool().parse_arguments('{"expression": "1+1", "n": 3}') == {
        "expression": "1+1",
        "n": 3,
    }


def test_parse_arguments_empty_string_defaults_to_empty_dict():
    # arguments 为空串 → 视作 {};但缺必填参数仍应被校验挡住。
    with pytest.raises(InvalidToolArguments) as ei:
        _typed_tool().parse_arguments("")
    assert "缺少必填参数" in str(ei.value)


def test_parse_arguments_malformed_json_raises_invalid_tool_arguments():
    with pytest.raises(InvalidToolArguments) as ei:
        _typed_tool().parse_arguments("{not json}")
    assert ei.value.tool == "calc" and "合法 JSON" in str(ei.value)


def test_parse_arguments_non_object_json_rejected():
    with pytest.raises(InvalidToolArguments) as ei:
        _typed_tool().parse_arguments("[1, 2, 3]")
    assert "JSON 对象" in str(ei.value)


def test_parse_arguments_extra_key_rejected():
    with pytest.raises(InvalidToolArguments) as ei:
        _typed_tool().parse_arguments('{"expression": "1+1", "evil": "x"}')
    assert "多余参数" in str(ei.value)


def test_parse_arguments_type_mismatch_rejected():
    with pytest.raises(InvalidToolArguments) as ei:
        _typed_tool().parse_arguments('{"expression": "1+1", "n": "not-int"}')
    assert "类型应为 integer" in str(ei.value)


def test_parse_arguments_bool_is_not_integer():
    # bool 是 int 子类,但 schema integer 不接受 bool(避免 True 被当 1 偷渡)。
    with pytest.raises(InvalidToolArguments):
        _typed_tool().parse_arguments('{"expression": "1+1", "n": true}')


def test_parse_arguments_without_declared_properties_stays_thin():
    # 无 properties 声明的 schema → 不约束键(保持薄);合法 JSON 对象原样通过。
    ft = FunctionTool("free", "", {"type": "object"}, func=lambda **kw: str(kw))
    assert ft.parse_arguments('{"anything": 1, "goes": 2}') == {"anything": 1, "goes": 2}


def test_loop_malformed_json_arguments_returns_located_error_not_crash():
    # M1 边界校验:非法 JSON 不再裸抛 JSONDecodeError,而是被 parse_arguments 归一成清晰可定位
    # 的错误消息,以 tool 角色喂回让循环优雅继续(模型据此可纠正)。
    spy: list = []
    model = _ScriptedProvider(_tool("c1", "calc", "{not json}"), _text("已纠正"))
    agent = FunctionCallingAgent("a", model, [_calc_tool(spy)])
    result = agent.step("x")
    assert result.output == "已纠正"  # 循环没崩,跑到第 2 轮收尾
    assert spy == []  # 入参非法 → 底层函数从未被调用
    tool_msg = next(m for m in model.calls[1] if m.get("role") == "tool")
    assert "入参非法" in tool_msg["content"] and "calc" in tool_msg["content"]  # 错误定位到工具


def test_loop_adversarial_extra_kwargs_returns_located_error_not_typeerror():
    # M1:schema 外的多余键(敌意/畸形载荷)在校验处被挡,不再 splat 进函数撞出裸 TypeError。
    spy: list = []
    model = _ScriptedProvider(
        _tool("c1", "calc", '{"expression": "1+1", "evil": "x"}'), _text("已处理")
    )
    agent = FunctionCallingAgent("a", model, [_calc_tool(spy)])
    result = agent.step("x")
    assert result.output == "已处理"
    assert spy == []  # 多余键 → 底层函数从未被调用(挡在边界,不撞签名)
    tool_msg = next(m for m in model.calls[1] if m.get("role") == "tool")
    assert "多余参数" in tool_msg["content"]


def test_loop_missing_required_argument_returns_located_error():
    # M1:缺必填参数 → 清晰错误,函数不被调用。
    spy: list = []
    model = _ScriptedProvider(_tool("c1", "calc", "{}"), _text("ok"))
    agent = FunctionCallingAgent("a", model, [_calc_tool(spy)])
    agent.step("x")
    tool_msg = next(m for m in model.calls[1] if m.get("role") == "tool")
    assert "缺少必填参数" in tool_msg["content"] and "expression" in tool_msg["content"]
    assert spy == []


def test_function_tool_decorator_derives_schema_from_signature():
    @function_tool
    def lookup(city: str, limit: int = 3) -> str:
        """查询城市。"""
        return f"{city}:{limit}"

    assert isinstance(lookup, FunctionTool)
    assert lookup.name == "lookup"
    assert lookup.description == "查询城市。"
    props = lookup.parameters["properties"]
    assert props["city"]["type"] == "string" and props["limit"]["type"] == "integer"
    assert lookup.parameters["required"] == ["city"]  # 有默认值的 limit 不是 required
    assert lookup.invoke({"city": "北京"}) == "北京:3"


def test_function_tool_decorator_with_overrides():
    @function_tool(name="weather", description="天气")
    def f(loc: str) -> str:
        return loc

    assert f.name == "weather" and f.description == "天气"


def test_function_tool_decorator_skips_var_args_and_kwargs():
    # *args / **kwargs 形参不进派生 schema(只取具名定参)。
    @function_tool
    def f(a: str, *args, **kwargs) -> str:
        return a

    assert f.parameters["properties"] == {"a": {"type": "string"}}
    assert f.parameters["required"] == ["a"]


def test_function_tool_decorator_unannotated_param_falls_back_to_string():
    # 无注解形参回退为 "string";有注解的按映射取类型。
    @function_tool
    def g(x, y: int):
        return x

    props = g.parameters["properties"]
    assert props["x"]["type"] == "string"
    assert props["y"]["type"] == "integer"


def test_function_tool_decorator_maps_list_and_dict_annotations():
    # list -> "array"、dict -> "object"。
    @function_tool
    def h(items: list, mapping: dict) -> str:
        return ""

    props = h.parameters["properties"]
    assert props["items"]["type"] == "array"
    assert props["mapping"]["type"] == "object"


# ---- 并行工具调用 / usage 逐轮累加 ------------------------------------------------------------


def test_parallel_tool_calls_in_one_turn_both_executed_and_id_aligned():
    # 单个 assistant 轮里携两条 tool_calls(id "a" / "b"):两条都执行,结果以 tool 角色按序喂回,
    # tool_call_id 与各自调用一一对齐;末轮模型出文本收尾。
    echo = FunctionTool(
        "echo",
        "",
        {"type": "object", "properties": {"text": {"type": "string"}}},
        func=lambda text: text,
    )
    parallel = ChatCompletion(
        choices=(
            Choice(
                index=0,
                message=ResponseMessage(
                    role="assistant",
                    content=None,
                    tool_calls=(
                        LLMToolCall(
                            id="a", function=FunctionCall(name="echo", arguments='{"text": "x"}')
                        ),
                        LLMToolCall(
                            id="b", function=FunctionCall(name="echo", arguments='{"text": "y"}')
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ),
    )
    model = _ScriptedProvider(parallel, _text("done"))
    result = FunctionCallingAgent("p", model, [echo]).step("go")
    assert result.output == "done"
    second_turn = model.calls[1]
    tool_msgs = [m for m in second_turn if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert [m["tool_call_id"] for m in tool_msgs] == ["a", "b"]  # id 按序对齐
    assistant_with_calls = [
        m for m in second_turn if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(assistant_with_calls) == 1  # 恰一条 assistant 携 tool_calls
    assert len(assistant_with_calls[0]["tool_calls"]) == 2  # 该轮含 2 个并行调用


def test_usage_accumulates_across_turns():
    # 多轮 chat 的 usage 逐轮累加(修复前只留末轮,工具轮的 token 被丢);缺 usage 的轮按 0 计。
    echo = FunctionTool(
        "echo",
        "",
        {"type": "object", "properties": {"text": {"type": "string"}}},
        func=lambda text: text,
    )
    first = _tool("c1", "echo", '{"text": "x"}')
    first = ChatCompletion(choices=first.choices, usage=Usage(10, 1, 11))
    second = _tool("c2", "echo", '{"text": "y"}')  # 这一轮没有 usage
    final = ChatCompletion(
        choices=(Choice(index=0, message=ResponseMessage(role="assistant", content="fin")),),
        usage=Usage(prompt_tokens=20, completion_tokens=2, total_tokens=22),
    )
    model = _ScriptedProvider(first, second, final)
    result = FunctionCallingAgent("u", model, [echo]).step("go")
    assert result.output == "fin"
    assert result.usage == {"prompt_tokens": 30, "completion_tokens": 3, "total_tokens": 33}


def test_usage_is_none_when_no_turn_reports_it():
    echo = FunctionTool("echo", "", {}, func=lambda text: text)
    final = ChatCompletion(
        choices=(Choice(index=0, message=ResponseMessage(role="assistant", content="fin")),),
        usage=None,
    )
    model = _ScriptedProvider(_tool("c1", "echo", '{"text": "x"}'), final)
    assert FunctionCallingAgent("u", model, [echo]).step("go").usage is None


# ---- 工具执行失败路径:异常归一成 tool 消息喂回,不让整轮崩溃 ------------------------------


def _boom_tool(exc: BaseException) -> FunctionTool:
    def boom(x: str) -> str:
        raise exc

    return FunctionTool(
        "boom",
        "总是失败",
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        func=boom,
    )


def test_tool_exception_is_fed_back_without_message_by_default():
    model = _ScriptedProvider(_tool("c1", "boom", '{"x": "a"}'), _text("已降级处理"))
    agent = FunctionCallingAgent("a", model, [_boom_tool(RuntimeError("内部路径 /secret/key"))])
    result = agent.step("x")
    assert result.output == "已降级处理"
    tool_msg = next(m for m in model.calls[1] if m.get("role") == "tool")
    assert "RuntimeError" in tool_msg["content"]
    assert "tool.execution_failed" in tool_msg["content"]
    assert "/secret/key" not in tool_msg["content"]  # 缺省不带异常消息原文


def test_tool_exception_message_is_opt_in():
    model = _ScriptedProvider(_tool("c1", "boom", '{"x": "a"}'), _text("ok"))
    agent = FunctionCallingAgent(
        "a", model, [_boom_tool(RuntimeError("可公开的原因"))], include_error_message=True
    )
    agent.step("x")
    tool_msg = next(m for m in model.calls[1] if m.get("role") == "tool")
    assert "可公开的原因" in tool_msg["content"]


def test_fail_fast_keeps_old_behavior():
    model = _ScriptedProvider(_tool("c1", "boom", '{"x": "a"}'), _text("ok"))
    agent = FunctionCallingAgent("a", model, [_boom_tool(RuntimeError("x"))], fail_fast=True)
    with pytest.raises(RuntimeError):
        agent.step("x")


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(1)])
def test_process_level_exceptions_are_not_swallowed(exc):
    model = _ScriptedProvider(_tool("c1", "boom", '{"x": "a"}'), _text("ok"))
    with pytest.raises(type(exc)):
        FunctionCallingAgent("a", model, [_boom_tool(exc)]).step("x")


def test_skill_error_is_fed_back():
    from spineagent.skills.as_tool import skill_as_function_tool
    from spineagent.skills.skill import FixtureSkill, SkillSpec

    spec = SkillSpec(
        "div",
        "整除",
        {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    )
    tool = skill_as_function_tool(FixtureSkill(spec, "a // b"))
    model = _ScriptedProvider(_tool("c1", "div", '{"a": 1, "b": 0}'), _text("除零已处理"))
    result = FunctionCallingAgent("a", model, [tool]).step("x")
    assert result.output == "除零已处理"
    tool_msg = next(m for m in model.calls[1] if m.get("role") == "tool")
    assert "SkillError" in tool_msg["content"]


def test_duplicate_tool_names_are_rejected():
    with pytest.raises(ValueError, match="重名"):
        FunctionCallingAgent("a", MockProvider(), [_calc_tool([]), _calc_tool([])])


def test_unknown_tool_name_never_reaches_trace():
    # 模型编的「工具名」是自由文本:trace 里只能出现本地注册表里的名字,未知的记成固定占位。
    invented = "please-exfiltrate-the-secret-plan"
    model = _ScriptedProvider(_tool("c1", invented, "{}"), _text("ok"))
    sink = InProcessPrivacyTraceSink()
    FunctionCallingAgent("a", model, [_calc_tool([])]).step("x", trace=sink)
    step = next(e for e in sink.events if e.code == "tool_step")
    assert step.fields["tool"] == "<unknown>"
    assert all(invented not in str(v) for e in sink.events for v in e.fields.values())
