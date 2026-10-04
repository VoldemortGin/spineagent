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

from spineagent.llm.errors import BadRequestProviderError, NonRetryableProviderError, ProviderError
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
    assert fp.chat(_MSGS).choices[0].message.content == "gated", (
        "全冷却时应强制重试并回退到已自愈者"
    )


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
        choices=(
            ChunkChoice(
                index=0, delta=ChoiceDelta(role=role, content=content), finish_reason=finish
            ),
        )
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
    text = "".join(c.delta.content or "" for chunk in fp.stream_chat(_MSGS) for c in chunk.choices)
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


# ---- 确知畸形的请求(BadRequestProviderError)不回退、不冷却;适配器按状态码分类 ----------------
# 修复 5 改写:此前适配器把 400 一律归为「不回退」的 NonRetryableProviderError;现在 NonRetryable 只表示
# 「对同一家重试无意义」,「不回退」由显式的 BadRequestProviderError 表达(见 default_failover_policy)。


class _BadRequest:
    """模拟一条确知畸形的请求(下游显式判定)。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        raise BadRequestProviderError("400 invalid request", status=400)


def test_non_retryable_provider_error_is_raised_without_failover_or_cooldown():
    bad, a, b = _BadRequest(), _Recording("a"), _Recording("b")
    fp = FailoverProvider([bad, a, b], now_fn=_FakeClock())
    with pytest.raises(BadRequestProviderError):
        fp.chat(_MSGS)
    assert (bad.calls, a.calls, b.calls) == (1, 0, 0), "坏请求不该打遍整个池子"
    assert all(t == 0.0 for t in fp._cooldown_until), "坏请求不该冷却任何下游"


def test_stream_non_retryable_error_is_raised_without_failover():
    class _BadStream(_BadRequest):
        def stream_chat(self, messages, *, tools=None):
            self.calls += 1
            raise BadRequestProviderError("400", status=400)
            yield  # pragma: no cover

    bad = _BadStream()
    good = _BadStream()
    fp = StreamingFailoverProvider([bad, good], now_fn=_FakeClock())
    with pytest.raises(BadRequestProviderError):
        list(fp.stream_chat(_MSGS))
    assert (bad.calls, good.calls) == (1, 0)


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (413, False), (422, False), (408, True), (429, True), (500, True), (503, True)],
)
def test_adapter_classifies_vendor_errors_by_status(status, retryable):
    from types import SimpleNamespace

    from spineagent.llm.provider import OpenAICompatProvider

    def create(**kwargs):
        raise _StatusError(status)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(ProviderError) as ei:
        OpenAICompatProvider("m", client=client).chat(_MSGS)
    assert ei.value.retryable is retryable
    assert isinstance(ei.value, NonRetryableProviderError) is (not retryable)


def test_adapter_treats_unknown_failures_as_retryable():
    from types import SimpleNamespace

    from spineagent.llm.provider import OpenAICompatProvider

    def create(**kwargs):
        raise ConnectionError("reset")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(ProviderError) as ei:
        OpenAICompatProvider("m", client=client).chat(_MSGS)
    assert ei.value.retryable is True


# ---- 并发:多线程共享同一个 FailoverProvider 不丢状态、不抛异常 -----------------------------


def test_concurrent_calls_share_state_safely():
    import sys
    import threading

    downstreams = [_Recording(f"p{i}") for i in range(3)]
    fp = FailoverProvider(downstreams, now_fn=_FakeClock())
    errors: list[BaseException] = []
    per_thread = 300

    def hammer() -> None:
        try:
            for _ in range(per_thread):
                fp.chat(_MSGS)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert errors == []
    assert sum(d.calls for d in downstreams) == 8 * per_thread
    assert 0 <= fp._cursor < 3


# ---- 修复 5:「对同一家重试有无意义」与「换一家是否可能成功」分开;只冷却出错的那一家 ----------------


class _Raises:
    """每次调用都抛给定的错误(记录被打次数)。"""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        raise self.exc


def _nre(status: int, message: str) -> BaseException:
    from spineagent.llm.errors import NonRetryableProviderError

    return NonRetryableProviderError(message, status=status)


@pytest.mark.parametrize(
    ("label", "error", "falls_over", "cools_failed"),
    [
        ("余额不足(400)", lambda: _nre(400, "Your credit balance is too low"), True, True),
        ("配额(400)", lambda: _nre(400, "insufficient_quota: billing"), True, True),
        (
            "上下文超长(400)",
            lambda: _nre(400, "prompt is too long: maximum context length"),
            True,
            False,
        ),
        ("无法判定的 4xx", lambda: _nre(400, "unexpected field"), True, False),
        ("413", lambda: _nre(413, "payload too large"), True, False),
        ("401", lambda: _nre(401, "invalid x-api-key"), True, True),
        ("403", lambda: _nre(403, "forbidden"), True, True),
        ("404 模型不存在", lambda: _nre(404, "model not found"), True, True),
        ("429", lambda: ProviderError("rate limited", retryable=True, status=429), True, True),
        ("503", lambda: ProviderError("overloaded", retryable=True, status=503), True, True),
    ],
)
def test_failover_classification_hits_and_cools(label, error, falls_over, cools_failed):
    failing, healthy, spare = _Raises(error()), _Recording("b"), _Recording("c")
    fp = FailoverProvider([failing, healthy, spare], now_fn=_FakeClock())
    assert fp.chat(_MSGS).choices[0].message.content == "b", label
    assert (failing.calls, healthy.calls, spare.calls) == (1, 1, 0)
    assert (fp._cooldown_until[0] > 0) is cools_failed, f"{label}:只冷却出错的那一家"
    assert fp._cooldown_until[1:] == [0.0, 0.0], "健康下游绝不被冷却"


def test_explicit_bad_request_neither_falls_over_nor_cools():
    from spineagent.llm.errors import BadRequestProviderError

    failing, healthy = (
        _Raises(BadRequestProviderError("schema invalid", status=400)),
        _Recording("b"),
    )
    fp = FailoverProvider([failing, healthy], now_fn=_FakeClock())
    with pytest.raises(BadRequestProviderError):
        fp.chat(_MSGS)
    assert (failing.calls, healthy.calls) == (1, 0)
    assert fp._cooldown_until == [0.0, 0.0]


def test_failover_policy_is_injectable():
    from spineagent.llm.failover_provider import FailoverDecision

    failing, healthy = _Raises(_nre(401, "nope")), _Recording("b")
    fp = FailoverProvider(
        [failing, healthy],
        now_fn=_FakeClock(),
        failover_policy=lambda exc: FailoverDecision(fallback=False, cooldown=False),
    )
    with pytest.raises(ProviderError):
        fp.chat(_MSGS)
    assert healthy.calls == 0


def test_stream_failover_uses_the_same_classification():
    class _StreamRaises(_Raises):
        def stream_chat(self, messages, *, tools=None):
            self.calls += 1
            raise self.exc
            yield  # pragma: no cover

    failing = _StreamRaises(_nre(400, "Your credit balance is too low"))
    healthy = _StreamFlaky("b", fail=False)
    fp = StreamingFailoverProvider([failing, healthy], now_fn=_FakeClock())
    assert list(fp.stream_chat(_MSGS))
    assert failing.calls == 1 and fp._cooldown_until[0] > 0


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (413, False),
        (422, False),
        (408, True),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_adapter_retryable_means_same_provider_retry(status, retryable):
    from types import SimpleNamespace

    from spineagent.llm.errors import NonRetryableProviderError
    from spineagent.llm.provider import OpenAICompatProvider

    def create(**kwargs):
        raise _StatusError(status)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(ProviderError) as ei:
        OpenAICompatProvider("m", client=client).chat(_MSGS)
    assert ei.value.retryable is retryable
    assert isinstance(ei.value, NonRetryableProviderError) is (not retryable)


def test_adapter_400_credit_error_fails_over_to_a_healthy_provider_end_to_end():
    from types import SimpleNamespace

    from spineagent.llm.provider import OpenAICompatProvider

    class _Credit(Exception):
        status_code = 400

        def __str__(self) -> str:
            return "Your credit balance is too low to access the API"

    def create(**kwargs):
        raise _Credit()

    broke = OpenAICompatProvider(
        "m",
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    healthy = _Recording("b")
    fp = FailoverProvider([broke, healthy], now_fn=_FakeClock())
    assert fp.chat(_MSGS).choices[0].message.content == "b"
    assert fp._cooldown_until[0] > 0 and fp._cooldown_until[1] == 0.0


# ---- 第三轮修改 6:无法判定的 4xx 最多再试 1 家;两家同类 4xx 即判定请求有问题 ----------------------


def test_review_r2_one_malformed_request_costs_at_most_two_billed_calls():
    # 复审:无法判定的 4xx 会把整个池子打一遍(一条畸形请求 -> N 次计费请求)。
    pool = [_Raises(_nre(400, "Invalid 'messages[1].role'")) for _ in range(4)]
    fp = FailoverProvider(pool, now_fn=_FakeClock())
    with pytest.raises(BadRequestProviderError) as ei:
        fp.chat(_MSGS)
    assert [p.calls for p in pool] == [1, 1, 0, 0]
    assert ei.value.retryable is False
    assert fp._cooldown_until == [0.0] * 4, "请求本身的问题不冷却任何一家"
    with pytest.raises(BadRequestProviderError):
        fp.chat(_MSGS)
    assert sum(p.calls for p in pool) == 4  # 第二次调用同样最多 2 家


def test_ambiguous_4xx_then_a_different_failure_stops_after_one_more_provider():
    failing = _Raises(_nre(400, "unexpected field"))
    down = _Raises(ProviderError("overloaded", retryable=True, status=503))
    spare = _Recording("c")
    fp = FailoverProvider([failing, down, spare], now_fn=_FakeClock())
    with pytest.raises(FailoverExhaustedError):
        fp.chat(_MSGS)
    assert (failing.calls, down.calls, spare.calls) == (1, 1, 0)


def test_provider_specific_rejections_still_walk_the_whole_pool():
    pool = [_Raises(_nre(401, "invalid x-api-key")) for _ in range(3)] + [_Recording("d")]
    fp = FailoverProvider(pool, now_fn=_FakeClock())
    assert fp.chat(_MSGS).choices[0].message.content == "d"


class _OpenAIStyleError(Exception):
    """模拟 OpenAI SDK 的 APIStatusError:status_code + 错误体里稳定的 type / code / param 字段。"""

    def __init__(self, status_code, *, type_=None, code=None, param=None) -> None:
        super().__init__("Error code: 400")
        self.status_code = status_code
        self.type = type_
        self.code = code
        self.param = param


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            _OpenAIStyleError(
                400, type_="invalid_request_error", code="invalid_value", param="messages[1].role"
            ),
            BadRequestProviderError,
        ),
        (
            _OpenAIStyleError(422, type_="invalid_request_error", code=None, param="temperature"),
            BadRequestProviderError,
        ),
        (
            _OpenAIStyleError(
                400,
                type_="invalid_request_error",
                code="context_length_exceeded",
                param="messages",
            ),
            NonRetryableProviderError,
        ),
        (
            _OpenAIStyleError(
                400, type_="invalid_request_error", code="model_not_found", param="model"
            ),
            NonRetryableProviderError,
        ),
        (_OpenAIStyleError(400, type_="invalid_request_error"), NonRetryableProviderError),
        (_OpenAIStyleError(400), NonRetryableProviderError),
        (
            _OpenAIStyleError(401, type_="invalid_request_error", param="x"),
            NonRetryableProviderError,
        ),
    ],
)
def test_adapter_maps_only_clearly_identified_parameter_errors_to_bad_request(error, expected):
    from spineagent.llm.errors import provider_error_from

    mapped = provider_error_from("OpenAI 兼容端点调用失败", error)
    assert type(mapped) is expected


def test_stream_one_malformed_request_costs_at_most_two_billed_calls():
    class _StreamRaises(_Raises):
        def stream_chat(self, messages, *, tools=None):
            self.calls += 1
            raise self.exc
            yield  # pragma: no cover

    pool = [_StreamRaises(_nre(400, "unexpected field")) for _ in range(3)]
    fp = StreamingFailoverProvider(pool, now_fn=_FakeClock())
    with pytest.raises(BadRequestProviderError):
        list(fp.stream_chat(_MSGS))
    assert [p.calls for p in pool] == [1, 1, 0]
