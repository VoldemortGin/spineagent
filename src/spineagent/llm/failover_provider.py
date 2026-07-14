"""组合式容错 LLM provider:包裹一组下游 provider,做轮询分摊 + 失败冷却 + 跨 provider 回退。

对标 Dify LBModelManager 的「多凭据轮询 + 撞错冷却」,博采其长自研并更进一步——它轮换的是【同一家
的多份凭据】,本 provider 轮换的是【任意异构下游 provider】(OpenAI / Anthropic / Cohere / …
任意 LLMProvider 混编),故天然支持【跨 provider 回退】:一家限流/挂了,自动切到下一家。

三条策略(见 `FailoverProvider.chat`):
  (a) 轮询(round-robin):游标每次成功后前移,把负载摊到各下游,不总压第一家;
  (b) 失败冷却:某下游抛【可重试错】(默认 = corespine ProviderError,即各适配器已把 vendor 的
      网络/超时/API 故障归一到的那个边界异常)后,进入冷却窗口(`cooldown_seconds`,默认 30s),
      窗口内的后续调用【跳过】它,不再徒劳打它;
  (c) 全冷却/全失败兜底:一次调用里,先按轮询序试【未冷却】的,若全失败,再【强制】按序试仍在
      冷却中的(它们也许已自愈);若连强制这轮也全失败,抛 `FailoverExhaustedError` 聚合错误,
      逐个列出下游失败原因——但【绝不含 API key / 凭据】(经 `_redact_credentials` 脱敏)。

【只对可重试错回退,绝不吞逻辑错】:catch 的是显式的 `retryable_errors`(默认 ProviderError),
【不】用裸 `except Exception`。KeyError / TypeError 等程序 bug 照常上抛、立即失败,绝不被容错外衣
掩盖成「换一家再试」——那只会把真正的代码缺陷藏进重试噪声里。

【流式的诚实】:仅当【全部】下游都实现 `StreamingLLMProvider` 时,工厂才返回带 `stream_chat` 的
`StreamingFailoverProvider`;混编(有下游不支持流式)时返回不带 `stream_chat` 的基类,`isinstance
(p, StreamingLLMProvider)` 如实报 False。理由见 `make_failover_provider` docstring。

【确定性可测】:冷却窗口用可注入的 `now_fn`(默认 time.monotonic),不直接读时钟;轮询序、冷却
边界、聚合错误全可在离线、零网络下用故障注入 provider 精确断言。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any, cast

from corespine.llm.provider import (
    ChatCompletion,
    ChatCompletionChunk,
    LLMProvider,
    StreamingLLMProvider,
)

from spineagent.llm.errors import ProviderError

# 冷却窗口默认时长(秒):某下游撞可重试错后,这段时间内的调用跳过它。30s 是限流/瞬时故障的
# 常见恢复量级,既不会长到白白闲置一家、也不会短到刚冷却就又去打它。
_DEFAULT_COOLDOWN_SECONDS = 30.0

# 凭据脱敏模式:聚合错误里逐个复述下游异常文本前,先抹掉像密钥/令牌的片段,杜绝把 API key
# 写进日志/异常。宁可多抹(*** 占位),绝不漏抹——错误消息的可读性远不如凭据不外泄重要。
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{6,}"),  # OpenAI / Anthropic 风格密钥(含 sk-ant-…)
    re.compile(r"AKIA[0-9A-Z]{12,}"),  # AWS Access Key Id
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),  # Authorization: Bearer <token>
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),  # key=... / token: ...
)
_REDACTED = "***"


def _redact_credentials(text: str) -> str:
    """抹掉文本里像 API key / 令牌 / 密码的片段,换成 `***`(聚合错误脱敏用)。"""
    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


class FailoverExhaustedError(ProviderError):
    """全部下游都失败(含强制回退)后抛出的聚合错误。

    继承 ProviderError:①消费者按 ProviderError 兜底即可一并接住;②多层 FailoverProvider 嵌套时,
    内层耗尽对外层【本身就是】一次可重试的 provider 故障,外层会据此继续回退到它自己的其它下游。
    消息逐个列出下游失败原因,但均经 `_redact_credentials` 脱敏,绝不含凭据。
    """


class FailoverProvider:
    """容错组合 LLMProvider:轮询分摊 + 失败冷却 + 全冷却时强制回退(见模块 docstring)。

    只实现非流式 `chat`(满足 corespine LLMProvider 协议);流式变体见 `StreamingFailoverProvider`。
    构造须给非空下游列表;`cooldown_seconds` 配冷却时长,`retryable_errors` 配「哪些异常算可重试」
    (默认仅 ProviderError),`now_fn` 注入时钟(默认 time.monotonic)以便确定性测试。
    """

    def __init__(
        self,
        providers: Sequence[LLMProvider],
        *,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
        retryable_errors: tuple[type[BaseException], ...] = (ProviderError,),
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        providers = tuple(providers)
        if not providers:
            raise ValueError("FailoverProvider 至少需要一个下游 provider")
        if cooldown_seconds < 0:
            raise ValueError(f"cooldown_seconds 不得为负:{cooldown_seconds}")
        self._providers = providers
        self._cooldown_seconds = cooldown_seconds
        self._retryable_errors = retryable_errors
        self._now = now_fn
        # 每个下游的冷却截止时刻(now_fn 时间轴);<= now 视为可用。初始全 0(均可用)。
        self._cooldown_until = [0.0] * len(providers)
        # 轮询游标:下一次调用【优先】从这个下游起试,每次成功后前移一位。
        self._cursor = 0

    def _attempt_order(self, now: float) -> list[int]:
        """本次调用的下游尝试序:先【未冷却】的(从游标起轮询),再【冷却中】的(强制兜底)。

        非冷却段实现策略 (a) 轮询分摊;冷却段附在末尾实现策略 (c) 的强制回退——冷却中的下游也许
        已自愈,全部未冷却的都失败时,宁可再赌一把强制试它们,也不直接放弃。每个下游本次至多试一次。
        """
        rotated = [(self._cursor + i) % len(self._providers) for i in range(len(self._providers))]
        not_cooled = [i for i in rotated if now >= self._cooldown_until[i]]
        cooled = [i for i in rotated if now < self._cooldown_until[i]]
        return not_cooled + cooled

    def _on_success(self, idx: int) -> None:
        """某下游成功:游标前移到它之后(轮询),并清掉它的冷却(已证明恢复)。"""
        self._cooldown_until[idx] = 0.0
        self._cursor = (idx + 1) % len(self._providers)

    def _on_failure(
        self, idx: int, exc: BaseException, now: float, failures: dict[int, str]
    ) -> None:
        """某下游撞可重试错:记录(脱敏)原因并置其冷却截止时刻。"""
        self._cooldown_until[idx] = now + self._cooldown_seconds
        label = type(self._providers[idx]).__name__
        failures[idx] = f"[{idx}] {label}: {_redact_credentials(str(exc))}"

    def _exhausted(self, failures: dict[int, str]) -> FailoverExhaustedError:
        """把逐个下游的(已脱敏)失败原因拼成一条清晰的聚合错误。"""
        reasons = "; ".join(failures[i] for i in sorted(failures))
        return FailoverExhaustedError(
            f"全部 {len(self._providers)} 个下游 provider 均失败:{reasons}"
        )

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        """按 `_attempt_order` 逐个下游试 chat;首个成功即返回,全失败则抛聚合错误。

        只 catch `retryable_errors`(默认 ProviderError):可重试错 → 记录 + 冷却 + 试下一家;
        其它异常(逻辑 bug)不 catch,照常上抛、立即失败,绝不被容错掩盖。
        """
        now = self._now()
        failures: dict[int, str] = {}
        for idx in self._attempt_order(now):
            try:
                result = self._providers[idx].chat(messages, tools=tools)
            except self._retryable_errors as exc:
                self._on_failure(idx, exc, now, failures)
                continue
            self._on_success(idx)
            return result
        raise self._exhausted(failures)


class StreamingFailoverProvider(FailoverProvider):
    """FailoverProvider 的流式变体:额外实现 `stream_chat`(StreamingLLMProvider 叠加协议)。

    仅当【全部】下游都支持流式时才由工厂选用(见 `make_failover_provider`),故 `stream_chat` 的
    回退池 == `chat` 的回退池,池里每个下游都能真流式——不会中途回退到一个吐不出块的下游,也不会
    让 `isinstance` 撒谎。
    """

    def stream_chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> Iterator[ChatCompletionChunk]:
        """与 chat 同款容错顺序,但走下游 stream_chat。

        生成器体在首次 next() 才执行,故先【预取首块】来触发下游:若在拿到首块前抛可重试错,可干净
        回退到下一家;一旦首块到手,就连同其余块顺序吐出。注:首块之后若下游【流到一半】才抛错,那时
        已吐出部分内容,无法重放,异常照常上抛——跨 provider 回退只在「尚未产出任何块」时可能。
        """
        now = self._now()
        failures: dict[int, str] = {}
        for idx in self._attempt_order(now):
            # 工厂只在【全下游都实现 StreamingLLMProvider】时才选本类,故此处 cast 是安全的诚实断言。
            provider = cast(StreamingLLMProvider, self._providers[idx])
            try:
                gen = provider.stream_chat(messages, tools=tools)
                first = next(gen)
            except StopIteration:  # 下游合法地吐了个空流:算成功,推进游标后正常结束
                self._on_success(idx)
                return
            except self._retryable_errors as exc:
                self._on_failure(idx, exc, now, failures)
                continue
            self._on_success(idx)
            yield first
            yield from gen
            return
        raise self._exhausted(failures)


def make_failover_provider(
    providers: Sequence[LLMProvider],
    *,
    cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
    retryable_errors: tuple[type[BaseException], ...] = (ProviderError,),
    now_fn: Callable[[], float] = time.monotonic,
) -> FailoverProvider:
    """按下游能力【诚实地】选类:全下游支持流式 → StreamingFailoverProvider,否则 → FailoverProvider。

    为何「全部支持才声明流式」(而非「always 声明、只在流式子集里 failover」):后者会让 `isinstance
    (p, StreamingLLMProvider)` 报 True,却在 stream_chat 时用一个比 chat 更小的回退池(甚至流式子集
    为空时纯撒谎),或中途回退到吐不出块的下游——两者都违背家族「isinstance 不许撒谎」的铁律。
    只在【整池都能流式】时声明,`chat` 与 `stream_chat` 的回退池完全一致、语义统一,且每个下游各自
    already 保证「流式拼接 == 非流式」,组合层只是转发某一个下游的流,等价性自然成立。
    """
    providers = tuple(providers)
    kwargs: dict[str, Any] = {
        "cooldown_seconds": cooldown_seconds,
        "retryable_errors": retryable_errors,
        "now_fn": now_fn,
    }
    if providers and all(isinstance(p, StreamingLLMProvider) for p in providers):
        return StreamingFailoverProvider(providers, **kwargs)
    return FailoverProvider(providers, **kwargs)
