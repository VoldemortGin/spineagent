"""LLM provider 调用的统一边界异常(vendor 网络/超时/API 异常归一到此)。

各真实适配器(Anthropic / OpenAI / Cohere / Gemini / Bedrock)的 SDK 调用点用 try/except 把
【vendor 抛出的网络/超时/API 异常】归一成 `ProviderError`,给上层一个稳定、可 grep 的边界异常,
而非五花八门的 SDK 私有异常类型。

【只归一 vendor 运行时故障,绝不兜底程序错】:KeyError / TypeError / AttributeError 这类
逻辑 bug 照常向上抛出——不退化成 except Exception 兜底,以免韧性外衣掩盖真正的代码缺陷。

rule-of-three 已触发:ragspine 与 spineagent 两个消费者重复同一块稳定面(同继承
corespine.CorespineError、同 code="provider.error"),据此把 ProviderError 提上了
corespine 0.1.1。本模块改为【从 corespine 再导出】,保留 `spineagent.llm.errors.ProviderError`
这一历史导入路径向后兼容;不再在本地定义。
"""

from __future__ import annotations

from corespine import ProviderError

__all__ = [
    "BadRequestProviderError",
    "NonRetryableProviderError",
    "ProviderError",
    "provider_error_from",
]

# 【两个不同的问题】retryable 只回答「对【同一个】provider 重试有没有意义」;「换【另一个】provider 是否
# 可能成功」由 FailoverProvider 的分类函数(failover_provider.default_failover_policy,可注入)回答。
# 瞬时故障:网络 / 超时 / 408 / 425 / 429 / 5xx —— 同一家稍后重试可能就好。
_TRANSIENT_STATUS = frozenset({408, 425, 429})


class NonRetryableProviderError(ProviderError):
    """vendor 以非瞬时的 4xx 拒绝了这次请求(retryable=False):对【同一家】原样重试没有意义。

    换另一家是否可能成功是另一个问题:余额 / 配额 / 鉴权 / 模型不存在 / 上下文超长这类与具体 provider
    相关的拒绝,换一家完全可能成功——FailoverProvider 缺省会回退(见 default_failover_policy)。
    在本包内用子类表达,而不改 corespine 的 ProviderError。
    """

    code = "provider.non_retryable"
    retryable = False


class BadRequestProviderError(NonRetryableProviderError):
    """请求本身畸形(如参数校验失败),换哪家都一样失败:FailoverProvider 不回退、不冷却,直接上抛。

    适配器在无法核实各家错误码的前提下【不会】自动判定出它(宁可多试一家);确知请求畸形的下游 /
    自定义分类函数显式使用它。
    """

    code = "provider.bad_request"


def _status_code(exc: BaseException) -> int | None:
    """尽力从各家 SDK 异常里取 HTTP 状态码(取不到返回 None)。"""
    for candidate in (
        getattr(exc, "status_code", None),  # openai / anthropic / cohere
        getattr(exc, "code", None),  # google-genai APIError
        getattr(getattr(exc, "response", None), "status_code", None),  # httpx 风格
    ):
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    response = getattr(exc, "response", None)
    if isinstance(response, dict):  # botocore ClientError
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(status, int):
            return status
    return None


def provider_error_from(message: str, exc: BaseException) -> ProviderError:
    """把 vendor 异常归一成 ProviderError,retryable 只表示「对同一家重试是否有意义」。

    网络 / 超时 / 408 / 425 / 429 / 5xx / 取不到状态码 -> ProviderError(retryable=True);其余 4xx
    (400 / 401 / 403 / 404 / 413 / 422 …)-> NonRetryableProviderError(retryable=False)。是否换一家由
    FailoverProvider 的分类函数决定。消息沿用调用方给的文本,状态码进 context。
    """
    status = _status_code(exc)
    if status is not None and 400 <= status < 500 and status not in _TRANSIENT_STATUS:
        return NonRetryableProviderError(message, status=status)
    return ProviderError(message, retryable=True, status=status)
