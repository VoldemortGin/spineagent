"""流式适配的单元测试:能力探测 + 事件翻译 + 错误归一 + 未接入的 provider 不谎报能力。

conformance 已把「各块 ChatCompletionChunk 形状 + 流式拼接 == 非流式」参数化钉死(见 test_conformance.py);
这里补 isinstance 能力探测、Anthropic 事件翻译细节、流式网络错归一 ProviderError、未接入 provider 的诚实。
"""

from types import SimpleNamespace

import pytest
from corespine.llm.provider import ChatCompletionChunk, StreamingLLMProvider

from spineagent.llm.bedrock_provider import BedrockConverseProvider
from spineagent.llm.cohere_provider import CohereProvider
from spineagent.llm.errors import ProviderError
from spineagent.llm.provider import AnthropicProvider, OpenAICompatProvider


class _OpenAIStreamClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self)

    def create(self, *, model, messages, max_tokens, stream=False, **extra):
        def _c(role=None, content=None, finish=None):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        index=0,
                        delta=SimpleNamespace(role=role, content=content),
                        finish_reason=finish,
                    )
                ],
                model=model,
                id="x",
                created=0,
            )

        return iter([_c(role="assistant"), _c(content="ab"), _c(content="cd"), _c(finish="stop")])


class _OpenAIBoomClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self)

    def create(self, *, model, messages, max_tokens, stream=False, **extra):
        raise RuntimeError("connreset")


class _AnthropicStreamClient:
    def __init__(self) -> None:
        self.messages = self

    def create(self, *, model, max_tokens, system, messages, stream=False, **extra):
        return iter(
            [
                SimpleNamespace(type="message_start"),
                SimpleNamespace(type="ping"),  # 无关事件应被忽略
                SimpleNamespace(
                    type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="ab")
                ),
                SimpleNamespace(
                    type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="cd")
                ),
                SimpleNamespace(
                    type="message_delta", delta=SimpleNamespace(stop_reason="max_tokens")
                ),
            ]
        )


def test_openai_provider_is_detected_as_streaming():
    provider = OpenAICompatProvider("gpt-x", client=_OpenAIStreamClient())
    assert isinstance(provider, StreamingLLMProvider)
    chunks = list(provider.stream_chat([{"role": "user", "content": "hi"}]))
    assert all(isinstance(c, ChatCompletionChunk) for c in chunks)
    text = "".join(c.delta.content or "" for ch in chunks for c in ch.choices)
    assert text == "abcd"


def test_anthropic_provider_translates_events_and_maps_finish():
    provider = AnthropicProvider(client=_AnthropicStreamClient())
    assert isinstance(provider, StreamingLLMProvider)
    chunks = list(provider.stream_chat([{"role": "user", "content": "hi"}]))
    text = "".join(c.delta.content or "" for ch in chunks for c in ch.choices)
    assert text == "abcd"
    # message_delta 的 stop_reason=max_tokens → OpenAI finish_reason=length。
    finishes = [c.finish_reason for ch in chunks for c in ch.choices if c.finish_reason]
    assert finishes == ["length"]


def test_stream_network_error_is_normalized_to_provider_error():
    provider = OpenAICompatProvider("gpt-x", client=_OpenAIBoomClient())
    with pytest.raises(ProviderError):
        list(provider.stream_chat([{"role": "user", "content": "hi"}]))


def test_non_streaming_providers_do_not_claim_streaming():
    # 诚实:未接入流式的 provider 不加会撒谎的桩,isinstance 如实报 False。
    assert not isinstance(CohereProvider(client=object()), StreamingLLMProvider)
    assert not isinstance(BedrockConverseProvider("m", client=object()), StreamingLLMProvider)
