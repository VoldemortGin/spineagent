"""LLM provider 适配层合约:Anthropic / OpenAI 兼容适配器的原生映射 + Registry + LlmAgent 集成。

离线:注入 fake client 验证「请求构造 + 响应映射」,绝不真连网络;真实 SDK 路径只是延迟 import。
"""

import importlib.util
from types import SimpleNamespace

import pytest
from corespine.llm.provider import Completion, LLMProvider, MockProvider

from agentspine.agent.agent import LlmAgent
from agentspine.llm.provider import (
    AnthropicProvider,
    OpenAICompatProvider,
    llm_providers,
    load_anthropic_sdk,
    load_openai_sdk,
)

# ---- fake 官方 SDK client(只实现适配器用到的最小面)-------------------------------------


class _FakeAnthropic:
    """伪 anthropic 客户端:messages.create 回 content blocks(含一个 thinking 空块)+ usage。"""

    def __init__(self) -> None:
        self.messages = self

    def create(self, *, model, max_tokens, system, messages, **extra):
        prompt = messages[-1]["content"]
        self.last = SimpleNamespace(model=model, system=system, prompt=prompt, extra=extra)
        return SimpleNamespace(
            content=[
                # 非文本块带【非空】正文:适配器若不按 type 过滤,就会把它拼进输出、被测试抓住。
                SimpleNamespace(type="thinking", text="THINK_LEAK"),
                SimpleNamespace(type="text", text=f"A[{system}]{prompt}"),
            ],
            usage=SimpleNamespace(input_tokens=len(prompt), output_tokens=7),
        )


class _FakeOpenAI:
    """伪 openai 客户端:chat.completions.create 回 choices[0].message.content + usage。"""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self)

    def create(self, *, model, messages, max_tokens, **extra):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user = next(m["content"] for m in messages if m["role"] == "user")
        self.last = SimpleNamespace(model=model, messages=messages, extra=extra)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=f"O[{system}]{user}"))],
            usage=SimpleNamespace(prompt_tokens=len(user), completion_tokens=9),
        )


# ---- Anthropic 适配器 ------------------------------------------------------------------


def test_anthropic_maps_text_blocks_and_usage():
    provider = AnthropicProvider(client=_FakeAnthropic(), model="claude-opus-4-8")
    result = provider.complete("你好", system="你是助手")
    assert isinstance(result, Completion)
    assert result.text == "A[你是助手]你好"  # 只取 text block,thinking 空块被跳过
    assert result.usage == {"input_tokens": 2, "output_tokens": 7}


def test_anthropic_sends_prompt_as_user_message_with_separate_system():
    fake = _FakeAnthropic()
    AnthropicProvider(client=fake).complete("q", system="sys")
    assert fake.last.system == "sys"  # system 单独传(Anthropic 原生形状)
    assert fake.last.prompt == "q"
    assert fake.last.model == "claude-opus-4-8"  # 默认模型


def test_anthropic_satisfies_llm_provider_protocol():
    assert isinstance(AnthropicProvider(client=_FakeAnthropic()), LLMProvider)


# ---- OpenAI 兼容适配器 ------------------------------------------------------------------


def test_openai_maps_choice_content_and_usage():
    provider = OpenAICompatProvider("gpt-x", client=_FakeOpenAI())
    result = provider.complete("hi", system="role")
    assert result.text == "O[role]hi"
    assert result.usage == {"input_tokens": 2, "output_tokens": 9}


def test_openai_omits_system_message_when_empty():
    fake = _FakeOpenAI()
    OpenAICompatProvider("gpt-x", client=fake).complete("hi")
    roles = [m["role"] for m in fake.last.messages]
    assert roles == ["user"]  # 无 system 时不塞空 system 消息


def test_openai_satisfies_llm_provider_protocol():
    assert isinstance(OpenAICompatProvider("gpt-x", client=_FakeOpenAI()), LLMProvider)


# ---- 与 LlmAgent 集成(provider 即「统一 invoke」)--------------------------------------


def test_providers_drive_llm_agent():
    agent = LlmAgent("worker", AnthropicProvider(client=_FakeAnthropic()), system="sys")
    result = agent.step("任务")
    assert result.agent == "worker"  # provenance
    assert result.output == "A[sys]任务"

    agent2 = LlmAgent("w2", OpenAICompatProvider("m", client=_FakeOpenAI()))
    assert agent2.step("任务2").output == "O[]任务2"


# ---- Registry(离线默认 mock + 真实后端 anthropic / openai)------------------------------


def test_registry_makes_mock_default():
    provider = llm_providers.make("mock")
    assert isinstance(provider, MockProvider)
    assert provider.complete("x").text  # 离线确定性,非空


def test_registry_makes_real_providers_with_injected_client():
    a = llm_providers.make("anthropic", client=_FakeAnthropic())
    o = llm_providers.make("openai", model="m", client=_FakeOpenAI())
    assert isinstance(a, AnthropicProvider)
    assert isinstance(o, OpenAICompatProvider)
    assert a.complete("hi").text == "A[]hi"
    assert o.complete("hi").text == "O[]hi"


def test_registry_lists_all_names():
    names = llm_providers.names()
    assert {"mock", "anthropic", "openai"} <= set(names)
    assert llm_providers.group == "corespine.llm"  # 第三方 provider 的 entry-point group


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
