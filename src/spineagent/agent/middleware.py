"""middleware 缝:Middleware 协议 + 有序链包裹任意 Agent(组合模式,包完还是 Agent)。

家族缝的元模式:Protocol + 离线确定性默认 + Registry 工厂 + 参数化 conformance。一个 Middleware
在 agent 的一步【前后】各挂一个钩子:before_step(ctx) 在执行前改写上下文(压缩 / 注入工具 / 挂附件
/ 记账),after_step(ctx, result) 在执行后加工结果。MiddlewareAgent 把一串 middleware 按序【洋葱式】
包住内层 Agent——before 正序、after 逆序——组合完仍是一个 Agent,可再进 Coordinator / 当工具 / 套链。

【隐私铁律】任何 middleware 只允许把【计数 / 长度 / 标志】记进 trace,绝不记任务 / 输出 / 附件正文
——由 corespine InProcessPrivacyTraceSink「构造即保证」兜底,本包再用 conformance 钉死(见
conformance.py 的 middleware 组:包裹后 trace 零正文泄漏)。离线内置四件套全部零网络、确定性。
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from corespine.llm.provider import LLMProvider, MockProvider
from corespine.observability.trace import TraceSink
from corespine.seam.registry import Registry

from spineagent.agent.agent import Agent, AgentResult


@dataclass
class StepContext:
    """一步执行的可变上下文:middleware 在 before_step 里改写它,内层 Agent 据 task 执行。

    task 可被 before_step 改写(如 SummaryMiddleware 压缩、AttachmentMiddleware 前置附件);tools /
    attachments 是 middleware 之间 + 与外层协作的显式共享面;step 是本 Agent 已迈出的步序(供
    DynamicToolMiddleware 按步调度);extras 留给自定义 middleware 传递零散元数据。绝不把这些正文
    写进 trace——trace 只记它们的计数 / 长度。
    """

    agent: str
    task: str
    trace: TraceSink | None = None
    step: int = 0
    tools: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Middleware(Protocol):
    """middleware 协议:在 agent 一步的前后各挂一个钩子。

    before_step 就地改写 ctx(返回 None);after_step 返回(可能被加工过的)AgentResult——须保留
    结果 provenance(result.agent),绝不吞掉来源。
    """

    def before_step(self, ctx: StepContext) -> None: ...

    def after_step(self, ctx: StepContext, result: AgentResult) -> AgentResult: ...


class MiddlewareAgent:
    """把一串 Middleware 按序洋葱式包住一个内层 Agent(实现 Agent 协议)。

    step():建 ctx → before_step 正序 → 内层 Agent.step(ctx.task, trace) → after_step 逆序 → 把结果
    provenance 重盖为本组合 agent 名(对外它就是产出者,子 provenance 是内部细节,与 AgentTool 同理)。
    顺序确定性:同一 (中间件列表, 输入) 恒定同一调用序;step 步序随每次 step() 单调增长。
    """

    def __init__(self, name: str, agent: Agent, middlewares: Iterable[Middleware]) -> None:
        self._name = name
        self._agent = agent
        self._middlewares = list(middlewares)
        self._step_index = 0

    @property
    def name(self) -> str:
        return self._name

    def step(self, task: str, *, trace: TraceSink | None = None) -> AgentResult:
        ctx = StepContext(agent=self._name, task=task, trace=trace, step=self._step_index)
        self._step_index += 1
        for mw in self._middlewares:
            mw.before_step(ctx)
        result = self._agent.step(ctx.task, trace=ctx.trace)
        for mw in reversed(self._middlewares):
            result = mw.after_step(ctx, result)
        # 重盖 provenance:对外产出者是本组合 agent(子 agent 名是内部细节)。
        return replace(result, agent=self._name)


# ---- 离线确定性内置四件套 ---------------------------------------------------------------------


def _whitespace_tokens(text: str) -> int:
    """默认 tokenizer:按空白切词计数(确定性、零依赖;可注入真实 tokenizer 覆盖)。"""
    return len(text.split())


class TokenUsageMiddleware:
    """记账 token 用量:入(ctx.task)/ 出(result.output)各计一次,只把【计数】记进隐私 trace。

    tokenizer 可插(默认按空白切词);累计总量存在实例 .totals,便于跨步汇总。绝不把正文写进 trace
    ——只发 mw_token_usage(input_tokens / output_tokens / step),字段全为计数。
    """

    def __init__(self, tokenizer: Callable[[str], int] | None = None) -> None:
        self._tokenizer = tokenizer if tokenizer is not None else _whitespace_tokens
        self.totals: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "steps": 0}

    def before_step(self, ctx: StepContext) -> None:
        count = self._tokenizer(ctx.task)
        self.totals["input_tokens"] += count
        ctx.extras["input_tokens"] = count

    def after_step(self, ctx: StepContext, result: AgentResult) -> AgentResult:
        out = self._tokenizer(result.output)
        self.totals["output_tokens"] += out
        self.totals["steps"] += 1
        if ctx.trace is not None:
            ctx.trace.emit(
                "mw_token_usage",
                agent=ctx.agent,
                step=ctx.step,
                input_tokens=ctx.extras.get("input_tokens", 0),
                output_tokens=out,
            )
        return result


class SummaryMiddleware:
    """上下文压缩:task 超过阈值时,走注入的 LLMProvider 生成摘要替换 ctx.task(离线用 MockProvider)。

    诚实取舍:是否真正「变短」取决于真实 provider;离线 MockProvider 只回声不压缩,但调用路径与真
    provider 完全一致(换后端不改一行)。只把长度记进 trace(orig_chars / summary_chars),绝不记正文。
    """

    def __init__(self, provider: LLMProvider | None = None, *, max_chars: int = 2000) -> None:
        self._provider = provider if provider is not None else MockProvider()
        self._max_chars = max_chars

    def before_step(self, ctx: StepContext) -> None:
        if len(ctx.task) <= self._max_chars:
            return
        completion = self._provider.chat(
            [{"role": "user", "content": f"用一段话概括以下内容:\n{ctx.task}"}]
        )
        summary = completion.choices[0].message.content or ""
        if ctx.trace is not None:
            ctx.trace.emit(
                "mw_summary",
                agent=ctx.agent,
                step=ctx.step,
                orig_chars=len(ctx.task),
                summary_chars=len(summary),
            )
        ctx.task = summary

    def after_step(self, ctx: StepContext, result: AgentResult) -> AgentResult:
        return result


class DynamicToolMiddleware:
    """按步注入 / 撤回工具:根据步序把「本步可用工具名集」解析进 ctx.tools(供协作方消费)。

    tools_by_step[step] 给出该步应激活的工具名列表;缺省回落到 default。这是一块【工具集调度】的
    机制积木——把可用面显式化在 ctx.tools 上,由协作的内层 agent / 下游 middleware 消费,不对内层
    Agent 施加隐式魔法。只把激活工具【数量】记进 trace,不记工具名之外的任何正文。
    """

    def __init__(
        self,
        tools_by_step: Mapping[int, Sequence[str]] | None = None,
        *,
        default: Sequence[str] = (),
    ) -> None:
        self._tools_by_step = dict(tools_by_step) if tools_by_step is not None else {}
        self._default = tuple(default)

    def before_step(self, ctx: StepContext) -> None:
        active = self._tools_by_step.get(ctx.step, self._default)
        ctx.tools = list(active)
        if ctx.trace is not None:
            ctx.trace.emit(
                "mw_dynamic_tools", agent=ctx.agent, step=ctx.step, tool_count=len(ctx.tools)
            )

    def after_step(self, ctx: StepContext, result: AgentResult) -> AgentResult:
        return result


class AttachmentMiddleware:
    """挂附件:把给定附件内容解析进 ctx.attachments 并前置到 ctx.task,让内层 Agent 看到它们。

    attachments 是 名字->内容 的映射;before_step 把全部内容前置进 task(供内层消费),并把内容存进
    ctx.attachments。只把附件【数量 / 总长度】记进 trace,绝不记附件正文。
    """

    def __init__(self, attachments: Mapping[str, str] | None = None) -> None:
        self._attachments = dict(attachments) if attachments is not None else {}

    def before_step(self, ctx: StepContext) -> None:
        if not self._attachments:
            return
        contents = list(self._attachments.values())
        ctx.attachments = contents
        header = "\n".join(f"[附件 {name}]\n{body}" for name, body in self._attachments.items())
        ctx.task = f"{header}\n\n{ctx.task}"
        if ctx.trace is not None:
            ctx.trace.emit(
                "mw_attachment",
                agent=ctx.agent,
                step=ctx.step,
                count=len(contents),
                total_chars=sum(len(c) for c in contents),
            )

    def after_step(self, ctx: StepContext, result: AgentResult) -> AgentResult:
        return result


# 缝注册表:一个 spec 选内置 middleware(各带离线确定性缺省,零参可造)。第三方亦可经 entry-point
# group "corespine.middleware" 装包发现自己的 middleware 工厂。
middlewares: Registry[Middleware] = Registry("middleware")
middlewares.register("token_usage", lambda **kw: TokenUsageMiddleware(**kw))
middlewares.register("summary", lambda **kw: SummaryMiddleware(**kw))
middlewares.register("dynamic_tool", lambda **kw: DynamicToolMiddleware(**kw))
middlewares.register("attachment", lambda **kw: AttachmentMiddleware(**kw))
