"""LLM provider 适配层合约:对外统一 OpenAI chat-completions 形状(含 tool-calling)+ Registry。

离线:注入 fake client 验证「请求构造 + 响应规整成 OpenAI ChatCompletion」,绝不真连网络。
OpenAI 适配器近 1:1;Anthropic 适配器把 native 响应【转成 OpenAI 形状】吐出(用户无感)。
"""

import importlib.util
import json
from types import SimpleNamespace

import pytest
from corespine.llm.provider import ChatCompletion, LLMProvider, MockProvider

from agentspine.agent.agent import LlmAgent
from agentspine.llm.provider import (
    AnthropicProvider,
    OpenAICompatProvider,
    llm_providers,
    load_anthropic_sdk,
    load_openai_sdk,
)

_OPENAI_TOOL = {
    "type": "function",
    "function": {"name": "calc", "description": "算术", "parameters": {"type": "object"}},
}


def _msgs(system: str, user: str) -> list[dict]:
    out = []
    if system:
        out.append({"role": "system", "content": system})
    out.append({"role": "user", "content": user})
    return out


# ---- fake 官方 SDK client ---------------------------------------------------------------


class _FakeAnthropic:
    """伪 anthropic 客户端:吃 Anthropic 原生形状(system 单独 + convo),回 content blocks + stop_reason。"""

    def __init__(self) -> None:
        self.messages = self

    def create(self, *, model, max_tokens, system, messages, tools=None, **extra):
        self.last = SimpleNamespace(system=system, messages=messages, tools=tools)
        user = messages[-1]["content"] if messages and isinstance(messages[-1]["content"], str) else ""
        if tools:
            content = [SimpleNamespace(type="tool_use", id="tu1", name="calc", input={"expr": "1+1"})]
            stop = "tool_use"
        else:
            content = [
                SimpleNamespace(type="thinking", text="THINK_LEAK"),  # 非文本块,应被过滤
                SimpleNamespace(type="text", text=f"A[{system}]{user}"),
            ]
            stop = "end_turn"
        return SimpleNamespace(
            content=content,
            stop_reason=stop,
            model="claude-x",
            id="msg_1",
            usage=SimpleNamespace(input_tokens=3, output_tokens=7),
        )


class _FakeOpenAI:
    """伪 openai 客户端:吃/回 OpenAI 原生形状(choices[].message + usage)。"""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self)

    def create(self, *, model, messages, max_tokens, tools=None, **extra):
        self.last = SimpleNamespace(model=model, messages=messages, tools=tools)
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user = next(m["content"] for m in messages if m["role"] == "user")
        if tools:
            tc = SimpleNamespace(
                id="tc1", function=SimpleNamespace(name="calc", arguments='{"expr": "1+1"}')
            )
            message = SimpleNamespace(role="assistant", content=None, tool_calls=[tc])
            finish = "tool_calls"
        else:
            message = SimpleNamespace(role="assistant", content=f"O[{system}]{user}", tool_calls=None)
            finish = "stop"
        return SimpleNamespace(
            choices=[SimpleNamespace(index=0, message=message, finish_reason=finish)],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=9, total_tokens=11),
            model=model,
            id="cmpl_1",
            created=0,
            object="chat.completion",
        )


# ---- Anthropic 适配器:native → OpenAI 形状 --------------------------------------------


def test_anthropic_output_is_openai_shaped():
    result = AnthropicProvider(client=_FakeAnthropic()).chat(_msgs("你是助手", "你好"))
    assert isinstance(result, ChatCompletion)
    msg = result.choices[0].message
    assert msg.content == "A[你是助手]你好"  # 只取 text block,thinking 块被过滤
    assert msg.tool_calls is None
    assert result.choices[0].finish_reason == "stop"  # end_turn → stop
    assert (result.usage.prompt_tokens, result.usage.completion_tokens, result.usage.total_tokens) == (3, 7, 10)


def test_anthropic_separates_system_into_native_shape():
    fake = _FakeAnthropic()
    AnthropicProvider(client=fake).chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}])
    assert fake.last.system == "sys"  # system 单独传(Anthropic 原生)
    assert fake.last.messages == [{"role": "user", "content": "q"}]


def test_anthropic_tool_use_becomes_openai_tool_calls():
    fake = _FakeAnthropic()
    result = AnthropicProvider(client=fake).chat(_msgs("", "算 1+1"), tools=[_OPENAI_TOOL])
    assert fake.last.tools == [{"name": "calc", "description": "算术", "input_schema": {"type": "object"}}]
    choice = result.choices[0]
    assert choice.finish_reason == "tool_calls"  # tool_use → tool_calls
    assert choice.message.content is None
    tc = choice.message.tool_calls[0]
    assert tc.type == "function" and tc.function.name == "calc"
    assert tc.function.arguments == json.dumps({"expr": "1+1"})  # arguments 是 JSON 串(OpenAI 一致)


def test_anthropic_satisfies_llm_provider_protocol():
    assert isinstance(AnthropicProvider(client=_FakeAnthropic()), LLMProvider)


# ---- OpenAI 兼容适配器:近 1:1 --------------------------------------------------------


def test_openai_output_is_openai_shaped():
    result = OpenAICompatProvider("gpt-x", client=_FakeOpenAI()).chat(_msgs("role", "hi"))
    assert result.choices[0].message.content == "O[role]hi"
    assert result.choices[0].finish_reason == "stop"
    assert result.choices[0].message.tool_calls is None
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (2, 9)


def test_openai_passes_messages_through_natively():
    fake = _FakeOpenAI()
    OpenAICompatProvider("gpt-x", client=fake).chat(_msgs("", "hi"))
    assert fake.last.messages == [{"role": "user", "content": "hi"}]


def test_openai_tool_calls_preserve_json_arguments():
    result = OpenAICompatProvider("gpt-x", client=_FakeOpenAI()).chat(_msgs("", "x"), tools=[_OPENAI_TOOL])
    choice = result.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.content is None
    assert choice.message.tool_calls[0].function.arguments == '{"expr": "1+1"}'  # 原样 JSON 串


def test_openai_satisfies_llm_provider_protocol():
    assert isinstance(OpenAICompatProvider("gpt-x", client=_FakeOpenAI()), LLMProvider)


# ---- 与 LlmAgent 集成(读 choices[0].message.content)----------------------------------


def test_providers_drive_llm_agent():
    agent = LlmAgent("worker", AnthropicProvider(client=_FakeAnthropic()), system="sys")
    result = agent.step("任务")
    assert result.agent == "worker"  # provenance
    assert result.output == "A[sys]任务"
    assert result.usage["prompt_tokens"] == 3  # usage 透到 AgentResult(OpenAI 键名)

    agent2 = LlmAgent("w2", OpenAICompatProvider("m", client=_FakeOpenAI()))
    assert agent2.step("任务2").output == "O[]任务2"


# ---- Registry --------------------------------------------------------------------------


def test_registry_makes_mock_default():
    provider = llm_providers.make("mock")
    assert isinstance(provider, MockProvider)
    assert provider.chat(_msgs("", "x")).choices[0].message.content  # 离线确定性,非空


def test_registry_makes_real_providers_with_injected_client():
    a = llm_providers.make("anthropic", client=_FakeAnthropic())
    o = llm_providers.make("openai", model="m", client=_FakeOpenAI())
    assert isinstance(a, AnthropicProvider)
    assert isinstance(o, OpenAICompatProvider)
    assert a.chat(_msgs("", "hi")).choices[0].message.content == "A[]hi"
    assert o.chat(_msgs("", "hi")).choices[0].message.content == "O[]hi"


def test_registry_lists_all_names():
    names = llm_providers.names()
    assert {"mock", "anthropic", "openai"} <= set(names)
    assert llm_providers.group == "corespine.llm"


def test_registry_unknown_spec_lists_available():
    with pytest.raises(ValueError) as ei:
        llm_providers.make("nope")
    msg = str(ei.value)
    assert "mock" in msg and "anthropic" in msg and "openai" in msg


# ---- 缺 extra 时延迟 import 给友好报错(仅当 SDK 确实未安装时断言)----------------------


@pytest.mark.skipif(
    importlib.util.find_spec("anthropic") is not None, reason="anthropic 已安装,无法验证缺失报错"
)
def test_anthropic_missing_extra_gives_friendly_error():
    with pytest.raises(ImportError) as ei:
        load_anthropic_sdk()
    assert "pip install agentspine[anthropic]" in str(ei.value)


@pytest.mark.skipif(
    importlib.util.find_spec("openai") is not None, reason="openai 已安装,无法验证缺失报错"
)
def test_openai_missing_extra_gives_friendly_error():
    with pytest.raises(ImportError) as ei:
        load_openai_sdk()
    assert "pip install agentspine[openai]" in str(ei.value)
