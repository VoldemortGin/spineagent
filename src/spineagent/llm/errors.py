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

__all__ = ["NonRetryableProviderError", "ProviderError", "provider_error_from"]

# 「请求本身有问题」的 4xx:换哪家 provider 重发都一样失败,重试 / 回退只会把坏请求打遍整个池子。
# 401 / 403 / 404 不在此列——鉴权 / 模型名是【各家各自的配置】,换一家可能就好,仍按可重试处理。
_NON_RETRYABLE_STATUS = frozenset({400, 413, 422})


class NonRetryableProviderError(ProviderError):
    """vendor 明确判定为坏请求的失败(retryable=False):FailoverProvider 不回退、不冷却,直接上抛。

    在本包内用子类表达「不可重试」,而不改 corespine:ProviderError 的类默认 retryable=False 被各处
    当作「可回退的 provider 故障」使用,无法用它区分;子类让判定显式、可 isinstance。
    """

    code = "provider.bad_request"
    retryable = False


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
    """把 vendor 异常归一成 ProviderError,并按状态码标 retryable(坏请求 -> 不可重试子类)。

    网络 / 超时 / 5xx / 限流 / 取不到状态码 -> 可重试(retryable=True);400 / 413 / 422 ->
    NonRetryableProviderError。消息沿用调用方给的文本,状态码进 context。
    """
    status = _status_code(exc)
    if status in _NON_RETRYABLE_STATUS:
        return NonRetryableProviderError(message, status=status)
    return ProviderError(message, retryable=True, status=status)
