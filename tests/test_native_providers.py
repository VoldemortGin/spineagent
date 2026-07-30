"""非 OpenAI 原生适配器合约:native 响应 → OpenAI ChatCompletion 映射(注入 fake client 离线验证)。

每家适配器:把 native 的 finish_reason(大写/蛇形)映射成 OpenAI 值、未知值落 stop;工具参数统一成
JSON 串;usage 字段重映射。这里只验【映射逻辑】,绝不真连网络。
"""

import importlib.util
import json
from types import SimpleNamespace

import pytest
from corespine.llm.provider import ChatCompletion, LLMProvider

from spineagent.llm.bedrock_provider import BedrockConverseProvider, load_boto3_sdk
from spineagent.llm.cohere_provider import CohereProvider, load_cohere_sdk
from spineagent.llm.errors import ProviderError
from spineagent.llm.gemini_provider import GeminiProvider, load_gemini_sdk
from spineagent.llm.provider import llm_providers

_OPENAI_TOOL = {"type": "function", "function": {"name": "calc", "parameters": {"type": "object"}}}

# 多轮 function-calling 历史(已含 assistant.tool_calls + tool 角色结果):喂给每家适配器,
# 验证「assistant.tool_calls + tool 角色」往返转成各家 native 形状(单轮历史从不覆盖这条缝)。
HISTORY = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "q"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "calc", "arguments": '{"x": 1}'}}
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "2"},
]


def _installed(module: str) -> bool:
    """模块是否可导入(find_spec 对缺父包的点路径会抛异常,这里一律吞成 False)。"""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


class _FakeCohere:
    """伪 cohere ClientV2:chat 回 Cohere v2 native 形状(content blocks / tool_calls / 大写 finish)。"""

    def __init__(self, *, finish_no_tool: str = "COMPLETE") -> None:
        self._finish_no_tool = finish_no_tool

    def chat(self, *, model, messages, tools=None, **extra):
        self.last = SimpleNamespace(model=model, messages=messages, tools=tools)
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        if tools:
            tc = SimpleNamespace(
                id="c1",
                type="function",
                function=SimpleNamespace(name="calc", arguments='{"x": 1}'),
            )
            message = SimpleNamespace(content=[], tool_calls=[tc])
            finish = "TOOL_CALL"
        else:
            message = SimpleNamespace(
                content=[SimpleNamespace(type="text", text=f"C:{user}")], tool_calls=None
            )
            finish = self._finish_no_tool
        usage = SimpleNamespace(tokens=SimpleNamespace(input_tokens=4, output_tokens=6))
        return SimpleNamespace(message=message, finish_reason=finish, usage=usage)


def test_cohere_text_maps_to_openai_shape():
    result = CohereProvider(client=_FakeCohere()).chat([{"role": "user", "content": "hi"}])
    assert isinstance(result, ChatCompletion)
    assert result.choices[0].message.content == "C:hi"  # content block 拼成 message.content
    assert result.choices[0].finish_reason == "stop"  # COMPLETE → stop
    assert result.choices[0].message.tool_calls is None
    assert (
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
        result.usage.total_tokens,
    ) == (4, 6, 10)


def test_cohere_tool_call_maps_to_openai_tool_calls():
    result = CohereProvider(client=_FakeCohere()).chat(
        [{"role": "user", "content": "算"}], tools=[_OPENAI_TOOL]
    )
    choice = result.choices[0]
    assert choice.finish_reason == "tool_calls"  # TOOL_CALL → tool_calls
    assert choice.message.content is None
    tc = choice.message.tool_calls[0]
    assert (tc.id, tc.type, tc.function.name) == ("c1", "function", "calc")
    assert tc.function.arguments == '{"x": 1}'  # Cohere v2 arguments 本就是 JSON 串,原样保留


def test_cohere_unknown_finish_reason_falls_back_to_stop():
    result = CohereProvider(client=_FakeCohere(finish_no_tool="SOMETHING_NEW")).chat(
        [{"role": "user", "content": "x"}]
    )
    assert result.choices[0].finish_reason == "stop"  # 未知 finish_reason 容忍落 stop


def test_cohere_multi_turn_tool_calls_pass_through_verbatim():
    # Cohere v2 直传 messages:多轮 assistant.tool_calls + tool 角色应原样到达 client(零转换)。
    fake = _FakeCohere()
    result = CohereProvider(client=fake).chat(HISTORY, tools=[_OPENAI_TOOL])
    assert fake.last.messages == HISTORY  # 逐字透传,assistant.tool_calls + tool 角色幸存
    choice = result.choices[0]
    assert choice.finish_reason == "tool_calls"  # TOOL_CALL → tool_calls
    assert choice.message.content is None
    tc = choice.message.tool_calls[0]
    assert (tc.id, tc.function.name) == ("c1", "calc")  # fake tool 分支回 id "c1"
    assert json.loads(tc.function.arguments) == {"x": 1}  # fake tool 分支回 '{"x": 1}'


def test_cohere_satisfies_protocol_and_registry():
    p = llm_providers.make("cohere", client=_FakeCohere())
    assert isinstance(p, CohereProvider)
    assert isinstance(p, LLMProvider)
    assert "cohere" in llm_providers.names()


@pytest.mark.skipif(_installed("cohere"), reason="cohere 已安装,无法验证缺失报错")
def test_cohere_missing_extra_gives_friendly_error():
    with pytest.raises(ImportError) as ei:
        load_cohere_sdk()
    assert "pip install spineagent[cohere]" in str(ei.value)


# ---- Gemini(google-genai)--------------------------------------------------------------


class _FakeGemini:
    """伪 google-genai Client:models.generate_content 回 Gemini native(candidates/parts/usageMetadata)。"""

    def __init__(self, *, with_tool: bool = False, finish: str = "STOP") -> None:
        self._with_tool = with_tool
        self._finish = finish
        self.models = self

    def generate_content(self, *, model, contents, config=None):
        self.last = SimpleNamespace(model=model, contents=contents, config=config)
        if self._with_tool:
            parts = [
                SimpleNamespace(
                    text=None, function_call=SimpleNamespace(name="calc", args={"x": 1})
                )
            ]
            finish = "STOP"
        else:
            parts = [SimpleNamespace(text="G:hi", function_call=None)]
            finish = self._finish
        candidate = SimpleNamespace(
            content=SimpleNamespace(parts=parts), finish_reason=SimpleNamespace(name=finish)
        )
        meta = SimpleNamespace(prompt_token_count=5, candidates_token_count=8, total_token_count=13)
        return SimpleNamespace(candidates=[candidate], usage_metadata=meta)


def test_gemini_text_maps_to_openai_shape():
    result = GeminiProvider(client=_FakeGemini()).chat([{"role": "user", "content": "hi"}])
    assert isinstance(result, ChatCompletion)
    assert result.choices[0].message.content == "G:hi"
    assert result.choices[0].finish_reason == "stop"
    assert (
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
        result.usage.total_tokens,
    ) == (5, 8, 13)


def test_gemini_function_call_maps_with_synthesized_id_and_json_args():
    fake = _FakeGemini(with_tool=True)
    result = GeminiProvider(client=fake).chat(
        [{"role": "user", "content": "算"}], tools=[_OPENAI_TOOL]
    )
    # 工具转成 Gemini function_declarations,且用 parameters_json_schema(标准 JSON Schema),非 parameters
    decl = fake.last.config["tools"][0]["function_declarations"][0]
    assert decl["name"] == "calc" and "parameters_json_schema" in decl and "parameters" not in decl
    choice = result.choices[0]
    assert choice.finish_reason == "tool_calls"  # 有 function_call → tool_calls
    assert choice.message.content is None
    tc = choice.message.tool_calls[0]
    assert tc.function.name == "calc"
    assert json.loads(tc.function.arguments) == {"x": 1}  # args dict → JSON 串
    assert tc.id  # Gemini 无 id,适配器自造


def test_gemini_safety_finish_maps_to_content_filter():
    result = GeminiProvider(client=_FakeGemini(finish="SAFETY")).chat(
        [{"role": "user", "content": "x"}]
    )
    assert result.choices[0].finish_reason == "content_filter"


def test_gemini_multi_turn_tool_calls_round_trip():
    # 多轮历史 → Gemini contents 形状,且 native function_call 响应 → OpenAI tool_calls。
    fake = _FakeGemini(with_tool=True)
    result = GeminiProvider(client=fake).chat(HISTORY, tools=[_OPENAI_TOOL])
    assert fake.last.config["system_instruction"] == "sys"  # system → system_instruction
    assert (
        fake.last.contents
        == [
            {"role": "user", "parts": [{"text": "q"}]},
            {"role": "model", "parts": [{"function_call": {"name": "calc", "args": {"x": 1}}}]},
            {
                "role": "user",
                "parts": [{"function_response": {"name": "c1", "response": {"result": "2"}}}],
            },
        ]
    )  # assistant.tool_calls→function_call、tool 角色→function_response(name 回落 tool_call_id "c1")
    choice = result.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.content is None
    tc = choice.message.tool_calls[0]
    assert tc.function.name == "calc" and tc.id  # Gemini 无 id,适配器自造
    assert json.loads(tc.function.arguments) == {"x": 1}  # fake tool 分支回 args {"x": 1}


def test_gemini_registry_and_protocol():
    p = llm_providers.make("gemini", client=_FakeGemini())
    assert isinstance(p, GeminiProvider) and isinstance(p, LLMProvider)
    assert "gemini" in llm_providers.names()


# ---- Bedrock Converse(boto3)----------------------------------------------------------


class _FakeBedrock:
    """伪 boto3 bedrock-runtime:converse 回 Converse native(output.message.content / stopReason / usage)。"""

    def __init__(self, *, with_tool: bool = False, stop: str = "end_turn") -> None:
        self._with_tool = with_tool
        self._stop = stop

    def converse(self, *, modelId, messages, **kwargs):
        self.last = SimpleNamespace(modelId=modelId, messages=messages, kwargs=kwargs)
        if self._with_tool:
            content = [{"toolUse": {"toolUseId": "b1", "name": "calc", "input": {"x": 1}}}]
            stop = "tool_use"
        else:
            content = [{"text": "B:hi"}]
            stop = self._stop
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop,
            "usage": {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8},
        }


def test_bedrock_text_maps_to_openai_shape():
    result = BedrockConverseProvider("anthropic.claude-x", client=_FakeBedrock()).chat(
        [{"role": "user", "content": "hi"}]
    )
    assert isinstance(result, ChatCompletion)
    assert result.choices[0].message.content == "B:hi"
    assert result.choices[0].finish_reason == "stop"  # end_turn → stop
    assert (
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
        result.usage.total_tokens,
    ) == (3, 5, 8)


def test_bedrock_tooluse_maps_to_openai_tool_calls():
    result = BedrockConverseProvider("m", client=_FakeBedrock(with_tool=True)).chat(
        [{"role": "user", "content": "算"}], tools=[_OPENAI_TOOL]
    )
    choice = result.choices[0]
    assert choice.finish_reason == "tool_calls"  # tool_use → tool_calls
    tc = choice.message.tool_calls[0]
    assert (tc.id, tc.function.name) == ("b1", "calc")
    assert json.loads(tc.function.arguments) == {"x": 1}  # toolUse.input dict → JSON 串


def test_bedrock_guardrail_maps_to_content_filter():
    result = BedrockConverseProvider("m", client=_FakeBedrock(stop="guardrail_intervened")).chat(
        [{"role": "user", "content": "x"}]
    )
    assert result.choices[0].finish_reason == "content_filter"


def test_bedrock_multi_turn_tool_calls_round_trip():
    # 多轮历史 → Bedrock Converse convo 形状,且 native toolUse 响应 → OpenAI tool_calls。
    fake = _FakeBedrock(with_tool=True)
    result = BedrockConverseProvider("m", client=fake).chat(HISTORY, tools=[_OPENAI_TOOL])
    assert fake.last.kwargs["system"] == [{"text": "sys"}]  # system → system 参数
    assert fake.last.messages == [
        {"role": "user", "content": [{"text": "q"}]},
        {
            "role": "assistant",
            "content": [{"toolUse": {"toolUseId": "c1", "name": "calc", "input": {"x": 1}}}],
        },
        {
            "role": "user",
            "content": [{"toolResult": {"toolUseId": "c1", "content": [{"text": "2"}]}}],
        },
    ]  # assistant.tool_calls→toolUse、tool 角色→toolResult
    choice = result.choices[0]
    assert choice.finish_reason == "tool_calls"  # tool_use → tool_calls
    assert choice.message.content is None
    tc = choice.message.tool_calls[0]
    assert (tc.id, tc.function.name) == ("b1", "calc")  # fake tool 分支回 id "b1"
    assert json.loads(tc.function.arguments) == {"x": 1}  # fake tool 分支回 input {"x": 1}


def test_bedrock_registry_and_protocol():
    p = llm_providers.make("bedrock", model="m", client=_FakeBedrock())
    assert isinstance(p, BedrockConverseProvider) and isinstance(p, LLMProvider)
    assert "bedrock" in llm_providers.names()


@pytest.mark.skipif(_installed("google.genai"), reason="google-genai 已安装")
def test_gemini_missing_extra_gives_friendly_error():
    with pytest.raises(ImportError) as ei:
        load_gemini_sdk()
    assert "pip install spineagent[gemini]" in str(ei.value)


@pytest.mark.skipif(_installed("boto3"), reason="boto3 已安装")
def test_bedrock_missing_extra_gives_friendly_error():
    with pytest.raises(ImportError) as ei:
        load_boto3_sdk()
    assert "pip install spineagent[bedrock]" in str(ei.value)


# ---- H1:三家原生适配器的 vendor 异常归一 + 程序错照常上抛 -----------------------------


class _BoomNetwork(Exception):
    """模拟 vendor SDK 抛出的网络/超时/API 异常(不真连网络)。"""


def test_cohere_vendor_exception_is_normalized_to_provider_error():
    class _Exploding:
        def chat(self, **kwargs):
            raise _BoomNetwork("cohere down")

    with pytest.raises(ProviderError) as ei:
        CohereProvider(client=_Exploding()).chat([{"role": "user", "content": "hi"}])
    assert isinstance(ei.value.__cause__, _BoomNetwork)


def test_gemini_vendor_exception_is_normalized_to_provider_error():
    class _Exploding:
        def __init__(self) -> None:
            self.models = self

        def generate_content(self, **kwargs):
            raise _BoomNetwork("gemini down")

    with pytest.raises(ProviderError) as ei:
        GeminiProvider(client=_Exploding()).chat([{"role": "user", "content": "hi"}])
    assert isinstance(ei.value.__cause__, _BoomNetwork)


def test_bedrock_vendor_exception_is_normalized_to_provider_error():
    class _Exploding:
        def converse(self, **kwargs):
            raise _BoomNetwork("bedrock down")

    with pytest.raises(ProviderError) as ei:
        BedrockConverseProvider("m", client=_Exploding()).chat([{"role": "user", "content": "hi"}])
    assert isinstance(ei.value.__cause__, _BoomNetwork)


def test_native_program_error_in_mapping_propagates_not_swallowed():
    # 响应映射期的程序错绝不被归一成 ProviderError —— 落在 try 之外照常上抛。
    # Cohere:网络成功但响应缺 .message 字段 → 映射期 AttributeError。
    class _BadCohere:
        def chat(self, **kwargs):
            return SimpleNamespace(finish_reason="COMPLETE")  # 无 .message

    with pytest.raises(AttributeError):
        CohereProvider(client=_BadCohere()).chat([{"role": "user", "content": "hi"}])

    # Bedrock:网络成功但响应是缺 "output" 键的 dict → 映射期 KeyError。
    class _BadBedrock:
        def converse(self, **kwargs):
            return {"stopReason": "end_turn"}  # 无 "output"

    with pytest.raises(KeyError):
        BedrockConverseProvider("m", client=_BadBedrock()).chat([{"role": "user", "content": "hi"}])
