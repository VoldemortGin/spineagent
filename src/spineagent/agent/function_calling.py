"""真 function-calling 的 agent:用一个 chat 模型的【原生工具调用】跑多步循环。

与离线确定性的 ToolUsingAgent(SyntaxToolPolicy 按语法路由)不同,FunctionCallingAgent 让【真 LLM】
自己决定调哪个工具:把 FunctionTool 的 schema 喂给 model.chat(tools=...),模型回 tool_calls →
按名执行工具 → 把结果以 OpenAI tool 角色消息喂回 → 再 chat,直到模型不再要工具(出文本)或触顶
max_steps。它实现 Agent 协议,故可直接进 Coordinator / 被 AgentTool 当工具 / 套进 ChainAgent。

对外只认 corespine 的 OpenAI-canonical chat 缝(ChatCompletion + OpenAI message dicts),所以底层换
任意 provider(OpenAI 兼容 / Anthropic / Gemini / Bedrock / Cohere)都不改这里一行——「统一 invoke」。
离线默认 MockProvider 不回 tool_calls,故它直接出文本(诚实:离线不假装会 function-calling)。

隐私:每步发 tool_step(agent / 步序 / 工具名 / 入参长度 / 输出长度)、收尾发 agent_finish、触顶发
agent_step_limit——只记 code / 计数,绝不记任务 / 参数 / 输出正文。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from corespine.llm.provider import LLMProvider
from corespine.observability.trace import TraceSink

from spineagent.agent.agent import AgentResult
from spineagent.tools.function_tool import FunctionTool

# 触顶 max_steps 仍未出最终文本时的兜底文案(保证产出非空)。
_NO_OUTPUT = "(reached max_steps without a final answer)"


class FunctionCallingAgent:
    """用真 LLM 的原生 function-calling 在单次 step() 内多步调用工具的 agent(实现 Agent 协议)。"""

    def __init__(
        self,
        name: str,
        model: LLMProvider,
        tools: Iterable[FunctionTool],
        *,
        system: str = "",
        max_steps: int = 8,
    ) -> None:
        self._name = name
        self._model = model
        self._tools = {tool.name: tool for tool in tools}
        self._system = system
        self._max_steps = max_steps

    @property
    def name(self) -> str:
        return self._name

    def step(self, task: str, *, trace: TraceSink | None = None) -> AgentResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        if self._system:
            messages.insert(0, {"role": "system", "content": self._system})
        schemas = [tool.schema() for tool in self._tools.values()] or None
        last_usage: dict[str, int] | None = None
        for index in range(self._max_steps):
            result = self._model.chat(messages, tools=schemas)
            last_usage = _usage_dict(result.usage)
            message = result.choices[0].message
            tool_calls = message.tool_calls or ()
            if not tool_calls:
                _emit_finish(trace, self._name, index, message.content or "")
                return AgentResult(self._name, message.content or "", usage=last_usage)
            # 把这一轮的 assistant(带 tool_calls)按 OpenAI 形状追加进对话历史。
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in tool_calls
                    ],
                }
            )
            # 逐个执行工具,把结果以 tool 角色消息喂回(tool_call_id 对齐)。
            for tc in tool_calls:
                tool = self._tools.get(tc.function.name)
                arguments = tc.function.arguments or "{}"
                if tool is None:
                    output = f"error: unknown tool {tc.function.name!r}"
                else:
                    output = tool.invoke(json.loads(arguments))
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
                _emit_tool_step(trace, self._name, index, tc.function.name, arguments, output)
        # 触顶 max_steps 仍在要工具:强制收尾(兜底非空)。
        _emit_step_limit(trace, self._name, self._max_steps)
        _emit_finish(trace, self._name, self._max_steps, _NO_OUTPUT)
        return AgentResult(self._name, _NO_OUTPUT, usage=last_usage)


def _usage_dict(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


def _emit_tool_step(
    trace: TraceSink | None, name: str, step: int, tool: str, arguments: str, output: str
) -> None:
    """隐私安全步级 trace:agent 名 / 步序 / 工具名 / 入参与输出长度,绝不记正文。"""
    if trace is None:
        return
    trace.emit(
        "tool_step", agent=name, step=step, tool=tool, arg_chars=len(arguments), output_chars=len(output)
    )


def _emit_finish(trace: TraceSink | None, name: str, steps: int, answer: str) -> None:
    if trace is None:
        return
    trace.emit("agent_finish", agent=name, steps=steps, answer_chars=len(answer))


def _emit_step_limit(trace: TraceSink | None, name: str, max_steps: int) -> None:
    if trace is None:
        return
    trace.emit("agent_step_limit", agent=name, max_steps=max_steps)
