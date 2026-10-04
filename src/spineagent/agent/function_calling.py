"""真 function-calling 的 agent:用一个 chat 模型的【原生工具调用】跑多步循环。

与离线确定性的 ToolUsingAgent(SyntaxToolPolicy 按语法路由)不同,FunctionCallingAgent 让【真 LLM】
自己决定调哪个工具:把 FunctionTool 的 schema 喂给 model.chat(tools=...),模型回 tool_calls →
按名执行工具 → 把结果以 OpenAI tool 角色消息喂回 → 再 chat,直到模型不再要工具(出文本)或触顶
max_steps。它实现 Agent 协议,故可直接进 Coordinator / 被 AgentTool 当工具 / 套进 ChainAgent。

对外只认 corespine 的 OpenAI-canonical chat 缝(ChatCompletion + OpenAI message dicts),所以底层换
任意 provider(OpenAI 兼容 / Anthropic / Gemini / Bedrock / Cohere)都不改这里一行——「统一 invoke」。
离线默认 MockProvider 不回 tool_calls,故它直接出文本(诚实:离线不假装会 function-calling)。

工具执行失败:工具函数抛的任何 Exception(审批错误除外)都归一成一条 tool 消息喂回模型,内容只含
稳定错误码与异常类型名(缺省不带异常消息原文——消息里可能有路径 / 凭据等敏感内容;
include_error_message=True 才附上),让循环优雅继续;fail_fast=True 恢复「异常直接冒泡」的旧行为。
审批错误(ApprovalError)与 KeyboardInterrupt / SystemExit 等非 Exception 一律不吞。

隐私:每步发 tool_step(agent / 步序 / 工具名 / 入参长度 / 输出长度)、收尾发 agent_finish、触顶发
agent_step_limit——只记 code / 计数,绝不记任务 / 参数 / 输出正文。工具名只取本地注册表里的名字,
模型编造的未知工具名记成固定占位 "<unknown>"。
"""

from collections.abc import Iterable
from typing import Any

from corespine.errors import CorespineError
from corespine.llm.provider import LLMProvider
from corespine.observability.trace import TraceSink

from spineagent.agent.agent import AgentResult, merge_usage
from spineagent.agent.approval import ApprovalError, enforce_tool_approval
from spineagent.tools.function_tool import FunctionTool, InvalidToolArguments
from spineagent.tools.tool import index_tools_by_name

# 触顶 max_steps 仍未出最终文本时的兜底文案(保证产出非空)。
_NO_OUTPUT = "(reached max_steps without a final answer)"

# trace 里未知工具名的固定占位:模型编的「工具名」是自由文本,绝不原样落进 trace。
UNKNOWN_TOOL = "<unknown>"

# 工具执行失败的稳定错误码(非 CorespineError 时使用;CorespineError 用它自己的 code)。
TOOL_EXECUTION_FAILED = "tool.execution_failed"


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
        fail_fast: bool = False,
        include_error_message: bool = False,
    ) -> None:
        self._name = name
        self._model = model
        self._tools = index_tools_by_name(tools)
        self._system = system
        self._max_steps = max_steps
        self._fail_fast = fail_fast
        self._include_error_message = include_error_message

    @property
    def name(self) -> str:
        return self._name

    def step(self, task: str, *, trace: TraceSink | None = None) -> AgentResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        if self._system:
            messages.insert(0, {"role": "system", "content": self._system})
        schemas = [tool.schema() for tool in self._tools.values()] or None
        # usage 逐轮累加(工具轮的 token 同样要算;缺 usage 的轮不计)。
        total_usage: dict[str, int] | None = None
        for index in range(self._max_steps):
            result = self._model.chat(messages, tools=schemas)
            total_usage = merge_usage(total_usage, _usage_dict(result.usage))
            message = result.choices[0].message
            tool_calls = message.tool_calls or ()
            if not tool_calls:
                _emit_finish(trace, self._name, index, message.content or "")
                return AgentResult(self._name, message.content or "", usage=total_usage)
            # 把这一轮的 assistant(带 tool_calls)按 OpenAI 形状追加进对话历史。
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
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
                    # 边界校验:LLM 发射的入参先按工具自带 schema 解析 + 校验再 splat;畸形/敌意
                    # 载荷给清晰可定位的错误消息(以 tool 角色喂回,让循环优雅继续),而非裸
                    # JSONDecodeError / TypeError 冒泡崩掉整轮。
                    try:
                        validated = tool.parse_arguments(arguments)
                    except InvalidToolArguments as exc:
                        output = f"error: {exc}"
                    else:
                        # 执行闸:每一次真实调用前按「真实工具名 + 参数」审批;未批准则抛错、不执行。
                        enforce_tool_approval(tool.name, validated)
                        output = self._invoke(tool, validated)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
                # trace 只记本地注册表里存在的工具名;模型编造的名字记成固定占位。
                traced_tool = tool.name if tool is not None else UNKNOWN_TOOL
                _emit_tool_step(trace, self._name, index, traced_tool, arguments, output)
        # 触顶 max_steps 仍在要工具:强制收尾(兜底非空)。
        _emit_step_limit(trace, self._name, self._max_steps)
        _emit_finish(trace, self._name, self._max_steps, _NO_OUTPUT)
        return AgentResult(self._name, _NO_OUTPUT, usage=total_usage)

    def _invoke(self, tool: FunctionTool, arguments: dict[str, Any]) -> str:
        """执行一次工具;失败归一成可喂回模型的错误文本(见模块 docstring)。"""
        if self._fail_fast:
            return tool.invoke(arguments)
        try:
            return tool.invoke(arguments)
        except ApprovalError:
            raise  # 嵌套 agent 里的审批挂起 / 拒绝必须冒到调用方(HITL 恢复靠它)
        except Exception as exc:  # noqa: BLE001 —— 工具失败喂回模型,不让整轮崩溃
            return _tool_error_text(exc, include_message=self._include_error_message)


def _tool_error_text(exc: Exception, *, include_message: bool) -> str:
    """工具失败的喂回文本:稳定错误码 + 异常类型名;仅显式开启时才附异常消息原文。"""
    code = exc.code if isinstance(exc, CorespineError) else TOOL_EXECUTION_FAILED
    text = f"error: tool failed [code={code} type={type(exc).__name__}]"
    return f"{text}: {exc}" if include_message else text


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
        "tool_step",
        agent=name,
        step=step,
        tool=tool,
        arg_chars=len(arguments),
        output_chars=len(output),
    )


def _emit_finish(trace: TraceSink | None, name: str, steps: int, answer: str) -> None:
    if trace is None:
        return
    trace.emit("agent_finish", agent=name, steps=steps, answer_chars=len(answer))


def _emit_step_limit(trace: TraceSink | None, name: str, max_steps: int) -> None:
    if trace is None:
        return
    trace.emit("agent_step_limit", agent=name, max_steps=max_steps)
