"""FailoverProvider 单元测试:轮询分摊 + 失败冷却 + 强制回退 + 聚合错误脱敏 + 流式诚实。

conformance 已把「LLMProvider 形状 / streamed==non-streamed」参数化钉死(见 test_conformance.py);
这里用可注入时钟 + 故障注入 provider,离线、零网络地精确断言容错策略的每一条分支。
"""

import pytest
from corespine.llm.provider import (
    ChatCompletion,
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
    ChunkChoice,
    ResponseMessage,
    StreamingLLMProvider,
)

from spineagent.llm.errors import ProviderError
from spineagent.llm.failover_provider import (
    FailoverExhaustedError,
    FailoverProvider,
    StreamingFailoverProvider,
    make_failover_provider,
)
from spineagent.llm.provider import llm_providers

_MSGS = [{"role": "user", "content": "hi"}]


def _completion(label: str) -> ChatCompletion:
    return ChatCompletion(
        choices=(Choice(index=0, message=ResponseMessage(role="assistant", content=label)),)
    )


class _FakeClock:
    """可注入时钟:测试自行推进,冷却窗口边界得以精确断言(不依赖真实 time)。"""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _Recording:
    """健康下游:记录调用次数,回一段带自身标签的补全(用于验证路由到了谁)。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        return _completion(self.label)


class _Flaky:
    """前 `fail_times` 次调用抛可重试 ProviderError,之后成功;secret 用于脱敏负向测试。"""

    def __init__(self, label: str, *, fail_times: int, secret: str = "") -> None:
        self.label = label
        self.remaining = fail_times
        self.secret = secret
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise ProviderError(f"{self.label} 限流 {self.secret}".strip())
        return _completion(self.label)


class _Gated:
    """由外部布尔控制:closed 时抛可重试错,open 时成功(测试强制回退到已冷却但已自愈的下游)。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.open = False
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        if not self.open:
            raise ProviderError(f"{self.label} 未就绪")
        return _completion(self.label)


class _Bomb:
    """抛【非】可重试错(逻辑 bug):必须原样上抛,绝不被容错吞掉、也绝不触发回退。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        raise ValueError("这是逻辑 bug,不是可重试错")


# ---- 策略 (a) 轮询分摊 ------------------------------------------------------------------------
def test_round_robin_distributes_across_downstreams():
    a, b, c = _Recording("a"), _Recording("b"), _Recording("c")
    fp = FailoverProvider([a, b, c], now_fn=_FakeClock())
    outputs = [fp.chat(_MSGS).choices[0].message.content for _ in range(6)]
    assert outputs == ["a", "b", "c", "a", "b", "c"], "成功调用应按轮询序均摊到各下游"
    assert a.calls == b.calls == c.calls == 2


# ---- 策略 (b) 失败冷却:窗口内跳过,窗口过后重新可用 -----------------------------------------
def test_failed_downstream_enters_cooldown_and_is_skipped():
    clock = _FakeClock()
    flaky, good = _Flaky("p0", fail_times=1), _Recording("p1")
    fp = FailoverProvider([flaky, good], cooldown_seconds=30.0, now_fn=clock)

    # 首调:p0 撞可重试错 → 冷却 + 回退到 p1。
    assert fp.chat(_MSGS).choices[0].message.content == "p1"
    assert flaky.calls == 1
    # 窗口内再调:p0 仍在冷却,被跳过(calls 不增),直接走 p1。
    assert fp.chat(_MSGS).choices[0].message.content == "p1"
    assert flaky.calls == 1, "冷却窗口内不得再打已冷却的下游"


def test_cooldown_expires_at_window_boundary():
    clock = _FakeClock()
    flaky, good = _Flaky("p0", fail_times=1), _Recording("p1")
    fp = FailoverProvider([flaky, good], cooldown_seconds=30.0, now_fn=clock)
    fp.chat(_MSGS)  # p0 冷却至 1030
    assert flaky.calls == 1

    clock.advance(29.0)  # 仍在窗口内
    fp.chat(_MSGS)
    assert flaky.calls == 1, "边界之前 p0 仍被跳过"

    clock.advance(1.0)  # 恰好到 1030,冷却到期
    out = fp.chat(_MSGS).choices[0].message.content
    assert flaky.calls == 2, "冷却到期后 p0 重新参与轮询"
    assert out == "p0", "p0 已自愈,应恢复成功"


# ---- 策略 (c) 全冷却时强制回退 + 聚合错误 ----------------------------------------------------
def test_all_cooled_forces_retry_of_cooled_downstreams():
    clock = _FakeClock()
    dead, gated = _Flaky("dead", fail_times=99), _Gated("gated")
    fp = FailoverProvider([dead, gated], cooldown_seconds=30.0, now_fn=clock)

    # 首调:两个都失败(gated 尚 closed)→ 都冷却 → 聚合抛错。
    with pytest.raises(FailoverExhaustedError):
        fp.chat(_MSGS)

    # 窗口内(两个都还冷却着)但 gated 已自愈:强制回退这轮应把 gated 也试到并成功。
    clock.advance(5.0)
    gated.open = True
    assert fp.chat(_MSGS).choices[0].message.content == "gated", "全冷却时应强制重试并回退到已自愈者"


def test_exhausted_error_aggregates_reasons_without_leaking_credentials():
    secret = "sk-ant-SUPERSECRETKEY0123456789"
    p0 = _Flaky("p0", fail_times=99, secret=secret)
    p1 = _Flaky("p1", fail_times=99, secret=secret)
    fp = FailoverProvider([p0, p1], now_fn=_FakeClock())
    with pytest.raises(FailoverExhaustedError) as excinfo:
        fp.chat(_MSGS)
    msg = str(excinfo.value)
    assert secret not in msg, "聚合错误绝不得泄露 API key / 凭据"
    assert "***" in msg, "凭据片段应被脱敏为 ***"
    assert "p0" in msg and "p1" in msg, "聚合错误应逐个列出各下游失败原因"


# ---- 只对可重试错回退,绝不吞逻辑错 ----------------------------------------------------------
def test_non_retryable_error_propagates_and_stops_failover():
    bomb, good = _Bomb(), _Recording("good")
    fp = FailoverProvider([bomb, good], now_fn=_FakeClock())
    with pytest.raises(ValueError):
        fp.chat(_MSGS)
    assert good.calls == 0, "逻辑错必须立即上抛,绝不回退到下一家掩盖 bug"


# ---- 构造校验 ---------------------------------------------------------------------------------
def test_empty_downstreams_rejected():
    with pytest.raises(ValueError):
        FailoverProvider([])


def test_negative_cooldown_rejected():
    with pytest.raises(ValueError):
        FailoverProvider([_Recording("a")], cooldown_seconds=-1.0)


# ---- 流式:能力诚实 + 首块前失败可回退 -------------------------------------------------------
def _chunk(*, role=None, content=None, finish=None) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        choices=(ChunkChoice(index=0, delta=ChoiceDelta(role=role, content=content), finish_reason=finish),)
    )


class _StreamFlaky:
    """流式下游:fail 时在首块前抛可重试错;否则吐 role → 一段内容 → stop。"""

    def __init__(self, label: str, *, fail: bool) -> None:
        self.label = label
        self.fail = fail

    def chat(self, messages, *, tools=None):
        return _completion(self.label)

    def stream_chat(self, messages, *, tools=None):
        if self.fail:
            raise ProviderError(f"{self.label} 流式失败")
        yield _chunk(role="assistant")
        yield _chunk(content=self.label)
        yield _chunk(finish="stop")


class _ChatOnly:
    """只实现 chat 的下游(不支持流式):用于验证混编时组合层不谎报流式能力。"""

    def chat(self, messages, *, tools=None):
        return _completion("x")


def test_all_streaming_downstreams_yield_streaming_composite():
    fp = make_failover_provider([_StreamFlaky("a", fail=False), _StreamFlaky("b", fail=False)])
    assert isinstance(fp, StreamingFailoverProvider)
    assert isinstance(fp, StreamingLLMProvider), "全下游支持流式 → 组合层如实声明流式"


def test_mixed_downstreams_do_not_claim_streaming():
    fp = make_failover_provider([_StreamFlaky("a", fail=False), _ChatOnly()])
    assert not isinstance(fp, StreamingFailoverProvider)
    assert not isinstance(fp, StreamingLLMProvider), "混编时组合层不得谎报流式"
    assert not hasattr(fp, "stream_chat"), "非流式变体绝不带 stream_chat(isinstance 不许撒谎)"


def test_stream_chat_fails_over_before_first_chunk():
    clock = _FakeClock()
    p0, p1 = _StreamFlaky("p0", fail=True), _StreamFlaky("p1", fail=False)
    fp = make_failover_provider([p0, p1], now_fn=clock)
    text = "".join(
        c.delta.content or "" for chunk in fp.stream_chat(_MSGS) for c in chunk.choices
    )
    assert text == "p1", "首块前抛可重试错应干净回退到下一家的流"


# ---- Registry / 配置形状接入 -----------------------------------------------------------------
def test_registry_builds_failover_from_config():
    fp = llm_providers.make(
        "failover",
        downstreams=[{"spec": "mock"}, {"spec": "mock"}],
        cooldown_seconds=5.0,
    )
    assert isinstance(fp, FailoverProvider)
    assert fp._cooldown_seconds == 5.0
    assert fp.chat(_MSGS).choices[0].message.content, "经注册表装配的 failover 应可正常 chat"
