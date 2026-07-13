"""middleware 缝的单元测试:洋葱式顺序 + 四件套专属行为 + 注册表。

conformance 已把「before/after 形状 + provenance 保全 + 包裹后确定性 + 包裹后 trace 零泄漏」参数化
钉死(见 test_conformance.py);这里补链的洋葱顺序、各 middleware 的具体效果、注册表。
"""

from corespine.llm.provider import MockProvider
from corespine.observability.trace import InProcessPrivacyTraceSink

from spineagent.agent.agent import AgentResult, FunctionAgent
from spineagent.agent.middleware import (
    AttachmentMiddleware,
    DynamicToolMiddleware,
    MiddlewareAgent,
    StepContext,
    SummaryMiddleware,
    TokenUsageMiddleware,
    middlewares,
)


class _SpyMiddleware:
    """记录 before/after 调用顺序的探针 middleware(验洋葱式包裹次序)。"""

    def __init__(self, tag: str, log: list[str]) -> None:
        self._tag = tag
        self._log = log

    def before_step(self, ctx: StepContext) -> None:
        self._log.append(f"before:{self._tag}")

    def after_step(self, ctx: StepContext, result: AgentResult) -> AgentResult:
        self._log.append(f"after:{self._tag}")
        return result


def test_onion_order_before_forward_after_reverse():
    log: list[str] = []
    agent = MiddlewareAgent(
        "mw",
        FunctionAgent("inner", lambda t: "ok"),
        [_SpyMiddleware("a", log), _SpyMiddleware("b", log), _SpyMiddleware("c", log)],
    )
    agent.step("go")
    # before 正序,after 逆序(洋葱)。
    assert log == ["before:a", "before:b", "before:c", "after:c", "after:b", "after:a"]


def test_wrapper_is_still_an_agent_with_rewrapped_provenance():
    agent = MiddlewareAgent("outer", FunctionAgent("inner", lambda t: f"done:{t}"), [])
    result = agent.step("ping")
    assert result.output == "done:ping"
    assert result.agent == "outer"  # provenance 重盖为组合 agent


def test_token_usage_accumulates_counts_only_in_trace():
    mw = TokenUsageMiddleware()
    sink = InProcessPrivacyTraceSink()
    MiddlewareAgent("mw", FunctionAgent("inner", lambda t: "a b c"), [mw]).step("x y", trace=sink)
    assert mw.totals["input_tokens"] == 2
    assert mw.totals["output_tokens"] == 3
    assert mw.totals["steps"] == 1
    # trace 只记计数,键名规避受限词表。
    usage_events = [e for e in sink.events if e.code == "mw_token_usage"]
    assert usage_events and usage_events[0].fields["output_tokens"] == 3


def test_token_usage_accepts_injected_tokenizer():
    mw = TokenUsageMiddleware(tokenizer=len)  # 按字符计
    MiddlewareAgent("mw", FunctionAgent("inner", lambda t: "abcd"), [mw]).step("xyz")
    assert mw.totals["input_tokens"] == 3
    assert mw.totals["output_tokens"] == 4


def test_summary_compresses_only_over_threshold():
    mw = SummaryMiddleware(MockProvider(), max_chars=5)
    ctx = StepContext(agent="mw", task="short")  # 恰好 5 字符 -> 不压缩
    mw.before_step(ctx)
    assert ctx.task == "short"
    ctx2 = StepContext(agent="mw", task="a very long task text")
    mw.before_step(ctx2)
    assert ctx2.task != "a very long task text"  # 被 provider 摘要替换


def test_dynamic_tool_injects_per_step_into_ctx():
    mw = DynamicToolMiddleware({0: ["calc"], 1: ["calc", "echo"]}, default=["noop"])
    ctx0 = StepContext(agent="mw", task="t", step=0)
    mw.before_step(ctx0)
    assert ctx0.tools == ["calc"]
    ctx2 = StepContext(agent="mw", task="t", step=2)  # 无调度 -> 回落 default
    mw.before_step(ctx2)
    assert ctx2.tools == ["noop"]


def test_attachment_prepends_content_and_records_counts():
    mw = AttachmentMiddleware({"notes": "hello"})
    sink = InProcessPrivacyTraceSink()
    ctx = StepContext(agent="mw", task="做点事", trace=sink)
    mw.before_step(ctx)
    assert "hello" in ctx.task and ctx.attachments == ["hello"]
    att_events = [e for e in sink.events if e.code == "mw_attachment"]
    assert att_events and att_events[0].fields["count"] == 1


def test_registry_makes_all_four_builtins():
    for name in ("token_usage", "summary", "dynamic_tool", "attachment"):
        mw = middlewares.make(name)
        assert hasattr(mw, "before_step") and hasattr(mw, "after_step")
    assert {"token_usage", "summary", "dynamic_tool", "attachment"} <= set(middlewares.names())
