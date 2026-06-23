"""LLM provider 调用的统一边界异常(vendor 网络/超时/API 异常归一到此)。

各真实适配器(Anthropic / OpenAI / Cohere / Gemini / Bedrock)的 SDK 调用点用 try/except 把
【vendor 抛出的网络/超时/API 异常】归一成 `ProviderError`,给上层一个稳定、可 grep 的边界异常,
而非五花八门的 SDK 私有异常类型。

【只归一 vendor 运行时故障,绝不兜底程序错】:KeyError / TypeError / AttributeError 这类
逻辑 bug 照常向上抛出——不退化成 except Exception 兜底,以免韧性外衣掩盖真正的代码缺陷。

rule-of-three 候选:ragspine 已在 `ragspine.agent.llm_provider` 本地定义同形 ProviderError
(同继承 corespine.CorespineError、同 code="provider.error")。两个消费者重复同一块稳定面 ——
未来可把 ProviderError 提上 corespine(再记一条 ADR);本次刻意只在 spineagent 本地定义,
不动 corespine / ragspine。
"""

from __future__ import annotations

from corespine import CorespineError


class ProviderError(CorespineError):
    """provider 调用失败的统一边界异常(网络/超时/API 错误归一到此)。

    只包裹 SDK 抛出的网络/API 异常;程序错误(KeyError/TypeError 等)不归此类,照常向上抛出,
    避免韧性兜底掩盖逻辑 bug。继承家族统一异常基类,稳定 code 为 "provider.error"(ADR errors 缝)。
    """

    code = "provider.error"
