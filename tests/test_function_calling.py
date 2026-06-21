"""真 function-calling agent 合约:LLM 回 tool_calls → 执行工具 → 喂回 → 再 chat → 出文本。

用一个【脚本化的 fake LLMProvider】(按预设依次返回 ChatCompletion)离线驱动多轮工具调用循环,
绝不真连网络。验证:工具确被执行、结果以 OpenAI tool 角色喂回、最终出文本;以及 schema / 装饰器 /
max_steps / 可组合 / 隐私 trace。
"""

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
from spineagent.tools.function_tool import FunctionTool, function_tool


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
        tool_calls=(LLMToolCall(id=call_id, function=FunctionCall(name=name, arguments=arguments)),),
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
        parameters={"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
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
        "echo", "回显", {"type": "object", "properties": {"text": {"type": "string"}}}, func=lambda text: text
    )
    model = _ScriptedProvider(_tool("c1", "echo", '{"text": "机密 2+3"}'), _text("机密结果"))
    sink = InProcessPrivacyTraceSink()
    FunctionCallingAgent("s", model, [echo]).step("机密任务", trace=sink)
    assert sink.codes() == ["tool_step", "agent_finish"]
    for event in sink.events:
        assert set(event.fields) <= {"agent", "step", "tool", "arg_chars", "output_chars", "steps", "answer_chars"}
        assert all("机密" not in str(v) for v in event.fields.values())  # 按值不泄露


# ---- FunctionTool / 装饰器 -------------------------------------------------------------


def test_function_tool_schema_is_openai_shape():
    ft = FunctionTool("calc", "算术", {"type": "object", "properties": {"x": {"type": "string"}}}, func=lambda x: x)
    s = ft.schema()
    assert s["type"] == "function"
    assert s["function"]["name"] == "calc"
    assert s["function"]["parameters"]["properties"]["x"]["type"] == "string"


def test_function_tool_invoke_calls_func_with_dict_args():
    ft = FunctionTool("add", "", {}, func=lambda a, b: a + b)
    assert ft.invoke({"a": 2, "b": 3}) == "5"  # 结果转字符串


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
