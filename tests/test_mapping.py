"""_mapping 中性中间层的纯单测:normalize_openai_messages / join_system / unwrap_function_tool。

只验解析骨架(parse 侧),离线确定、不连任何 API。各家 render 侧的逐字等价由 test_native_providers /
test_llm_provider 的往返断言覆盖,本文件只钉住中间表示本身的形状与边界(falsy content、缺参、
Gemini 的 name 怪癖)。
"""

from spineagent.llm._mapping import (
    AssistantToolCallsTurn,
    PlainTurn,
    SystemTurn,
    ToolCallPart,
    ToolResultTurn,
    join_system,
    normalize_openai_messages,
    unwrap_function_tool,
)


def test_system_turn_collected():
    turns = normalize_openai_messages([{"role": "system", "content": "sys"}])
    assert turns == [SystemTurn(text="sys")]


def test_system_falsy_content_becomes_empty_string():
    turns = normalize_openai_messages([{"role": "system", "content": None}])
    assert turns == [SystemTurn(text="")]


def test_join_system_joins_with_newline_only_system_turns():
    turns = normalize_openai_messages(
        [
            {"role": "system", "content": "a"},
            {"role": "user", "content": "q"},
            {"role": "system", "content": "b"},
        ]
    )
    assert join_system(turns) == "a\nb"  # 仅 SystemTurn 参与,按 "\n" 拼


def test_tool_result_turn_carries_id_content_and_name():
    turns = normalize_openai_messages(
        [{"role": "tool", "tool_call_id": "c1", "name": "calc", "content": "2"}]
    )
    assert turns == [ToolResultTurn(tool_call_id="c1", content="2", name="calc")]


def test_tool_result_name_falls_back_to_tool_call_id():
    # Gemini 怪癖:无 name 时 name 回落 tool_call_id(保证 Gemini function_response.name 逐字等价)。
    turns = normalize_openai_messages([{"role": "tool", "tool_call_id": "c1", "content": "2"}])
    assert turns == [ToolResultTurn(tool_call_id="c1", content="2", name="c1")]


def test_tool_result_falsy_content_becomes_empty_string():
    turns = normalize_openai_messages([{"role": "tool", "tool_call_id": "c1", "content": None}])
    assert turns[0] == ToolResultTurn(tool_call_id="c1", content="", name="c1")


def test_assistant_tool_calls_turn_decodes_arguments():
    turns = normalize_openai_messages(
        [
            {
                "role": "assistant",
                "content": "hi",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "calc", "arguments": '{"x": 1}'}}
                ],
            }
        ]
    )
    assert turns == [
        AssistantToolCallsTurn(
            text="hi",
            tool_calls=(ToolCallPart(id="c1", name="calc", arguments={"x": 1}),),
        )
    ]


def test_assistant_tool_calls_empty_content_becomes_text_none():
    # falsy content(None 或空串)→ text None(render 侧据此不吐文本 block)。
    for falsy in (None, ""):
        turns = normalize_openai_messages(
            [
                {
                    "role": "assistant",
                    "content": falsy,
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "calc", "arguments": '{"x": 1}'}}
                    ],
                }
            ]
        )
        assert turns[0].text is None


def test_assistant_tool_calls_missing_arguments_defaults_to_empty_dict():
    turns = normalize_openai_messages(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "function": {"name": "calc"}}],
            }
        ]
    )
    assert turns[0].tool_calls[0].arguments == {}  # 缺 arguments → {}


def test_assistant_tool_calls_null_arguments_defaults_to_empty_dict():
    turns = normalize_openai_messages(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "function": {"name": "calc", "arguments": None}}],
            }
        ]
    )
    assert turns[0].tool_calls[0].arguments == {}  # arguments=None → "{}"


def test_plain_user_turn():
    turns = normalize_openai_messages([{"role": "user", "content": "q"}])
    assert turns == [PlainTurn(role="user", content="q")]


def test_plain_assistant_turn_without_tool_calls():
    turns = normalize_openai_messages([{"role": "assistant", "content": "a"}])
    assert turns == [PlainTurn(role="assistant", content="a")]


def test_plain_unknown_role_normalizes_to_user():
    turns = normalize_openai_messages([{"role": "function", "content": "x"}])
    assert turns == [PlainTurn(role="user", content="x")]


def test_plain_falsy_content_becomes_empty_string():
    turns = normalize_openai_messages([{"role": "user", "content": None}])
    assert turns == [PlainTurn(role="user", content="")]


def test_unwrap_function_tool_with_function_wrapper():
    tool = {
        "type": "function",
        "function": {"name": "calc", "description": "算术", "parameters": {"type": "object"}},
    }
    assert unwrap_function_tool(tool) == ("calc", "算术", {"type": "object"})


def test_unwrap_function_tool_bare_body():
    tool = {"name": "calc"}
    assert unwrap_function_tool(tool) == (
        "calc",
        "",
        {"type": "object", "properties": {}},
    )  # 缺 description / parameters 用默认回落
