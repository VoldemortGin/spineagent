"""agent 缝:Agent 协议 + 最小默认实现(单步执行)。

家族缝的元模式:Protocol + 离线确定性默认 + 隐私安全 trace。Agent 是 agentspine 最小
的执行单元——给一个任务,跑【一步】拿回结果。两个默认实现都离线可跑、零网络:

  - LlmAgent —— 用一个 corespine LLMProvider 跑单步(离线用 MockProvider,确定性、可复现);
  - FunctionAgent —— 把一个纯函数 (task->text) 包成 Agent(无需 LLM,做测试/编排的轻量节点)。

隐私约定:step 可选接收一个 corespine TraceSink,实现只允许往里记【元数据】(agent 名、
长度、token 数),【绝不】记任务/输出正文——由 InProcessPrivacyTraceSink「构造即保证」,
本包再用 conformance 把这条不变量绑死(见 agentspine/conformance.py)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from corespine.llm.provider import LLMProvider
from corespine.observability.trace import TraceSink


@dataclass(frozen=True)
class AgentResult:
    """一次 agent 步的结果:产出文本 + 来源 agent 名(provenance)+ 可选 token 用量 / 错误。

    error 仅在【编排层弹性模式】下捕获 agent.step 异常时填充——归一为家族统一的可序列化错误
    dict(corespine.errors.error_to_dict:含 code / retryable / context)。正常成功路径 error 为
    None;agent.step 自身的契约仍是「成功产出非空、失败抛异常」,捕获与否是 Coordinator 的策略。
    """

    agent: str
    output: str
    usage: dict[str, int] | None = None
    error: dict[str, object] | None = None

    @property
    def ok(self) -> bool:
        """这步是否成功(未捕获到错误)。"""
        return self.error is None


@runtime_checkable
class Agent(Protocol):
    """agent 协议:有名字;给一个任务,跑【一步】拿回结果。

    step 可选接收一个 TraceSink:实现只允许往里记元数据(code/计数/耗时),绝不记任务/
    输出正文——隐私 by construction,由 corespine 的 InProcessPrivacyTraceSink 兜底。
    """

    name: str

    def step(self, task: str, *, trace: TraceSink | None = None) -> AgentResult: ...


class LlmAgent:
    """最小默认 agent:用一个 corespine LLMProvider 跑单步(离线用 MockProvider)。"""

    def __init__(self, name: str, provider: LLMProvider, *, system: str = "") -> None:
        self._name = name
        self._provider = provider
        self._system = system

    @property
    def name(self) -> str:
        return self._name

    def step(self, task: str, *, trace: TraceSink | None = None) -> AgentResult:
        messages: list[dict[str, str]] = [{"role": "user", "content": task}]
        if self._system:
            messages.insert(0, {"role": "system", "content": self._system})
        completion = self._provider.chat(messages)
        message = completion.choices[0].message
        usage = (
            {
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "total_tokens": completion.usage.total_tokens,
            }
            if completion.usage is not None
            else None
        )
        result = AgentResult(agent=self._name, output=message.content or "", usage=usage)
        _emit_step(trace, self._name, task, result)
        return result


class FunctionAgent:
    """最小确定性 agent:把一个纯函数 (task->text) 包成 Agent(离线/测试/编排用,无需 LLM)。"""

    def __init__(self, name: str, fn: Callable[[str], str]) -> None:
        self._name = name
        self._fn = fn

    @property
    def name(self) -> str:
        return self._name

    def step(self, task: str, *, trace: TraceSink | None = None) -> AgentResult:
        result = AgentResult(agent=self._name, output=self._fn(task))
        _emit_step(trace, self._name, task, result)
        return result


def _emit_step(
    trace: TraceSink | None, name: str, task: str, result: AgentResult
) -> None:
    """记一条隐私安全的步级 trace:只记 agent 名 + 长度 + token 数,绝不记正文。"""
    if trace is None:
        return
    usage = result.usage or {}
    # trace 字段名沿用 input/output_tokens(隐私元数据词表);取值兼容 OpenAI usage 的 prompt/
    # completion_tokens 与旧式 input/output_tokens 两种键。
    trace.emit(
        "agent_step",
        agent=name,
        task_chars=len(task),
        output_chars=len(result.output),
        input_tokens=usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        output_tokens=usage.get("completion_tokens", usage.get("output_tokens", 0)),
    )
