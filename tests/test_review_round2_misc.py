"""修复 7:usage 完整性、并行超时的可重试语义、CalcTool 的错误类型与深度语义、错误消息截断。"""

import pytest
from corespine.llm.provider import Usage

from spineagent.agent.agent import FunctionAgent
from spineagent.agent.builtin.deep_research import DeepResearchAgent
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.conformance import ScriptedToolCallProvider
from spineagent.orchestration.coordinator import AgentTimeoutError, Coordinator
from spineagent.tools.function_tool import FunctionTool, function_tool
from spineagent.tools.tool import CalcTool

_U = Usage(prompt_tokens=1, completion_tokens=10, total_tokens=11)


# ---- usage ------------------------------------------------------------------------------------------


def test_deep_research_usage_includes_retrieval_not_just_synthesis():
    # 审查:DeepResearchAgent 只返回综合阶段的 usage(实测 11,应为 55:4 条检索 + 1 次综合)。
    research = DeepResearchAgent(
        provider=ScriptedToolCallProvider([], usage=_U), planner=lambda t: ["a", "b", "c", "d"]
    )
    assert research.step("q").usage == {
        "prompt_tokens": 5,
        "completion_tokens": 50,
        "total_tokens": 55,
    }


def test_nested_agent_usage_inside_a_function_tool_is_counted():
    # 审查:FunctionTool 里嵌套的 agent 的 usage 丢失(实测 22,应为 44)。
    @function_tool
    def lookup(value: str) -> str:
        return value

    inner = FunctionCallingAgent(
        "inner", ScriptedToolCallProvider([("lookup", {"value": "x"})], usage=_U), [lookup]
    )

    def ask(question: str) -> str:
        return inner.step(question).output

    outer = FunctionCallingAgent(
        "outer",
        ScriptedToolCallProvider([("ask", {"question": "q"})], usage=_U),
        [
            FunctionTool(
                "ask", "", {"type": "object", "properties": {"question": {"type": "string"}}}, ask
            )
        ],
    )
    assert outer.step("go").usage["total_tokens"] == 44


def test_nested_usage_is_not_double_counted_through_composites():
    from spineagent.agent.middleware import MiddlewareAgent, TokenUsageMiddleware
    from spineagent.orchestration.chain import ChainAgent

    leaf = FunctionCallingAgent("leaf", ScriptedToolCallProvider([], usage=_U), [])
    wrapped = MiddlewareAgent("m", ChainAgent("c", [leaf, leaf]), [TokenUsageMiddleware()])

    def ask(question: str) -> str:
        return wrapped.step(question).output

    outer = FunctionCallingAgent(
        "outer",
        ScriptedToolCallProvider([("ask", {"question": "q"})], usage=_U),
        [
            FunctionTool(
                "ask", "", {"type": "object", "properties": {"question": {"type": "string"}}}, ask
            )
        ],
    )
    assert outer.step("go").usage["total_tokens"] == 22 + 22  # 外层 2 轮 + 嵌套 2 个叶子各 1 轮


# ---- 并行超时:线程不会被终止,结果不可重试 --------------------------------------------------------------


def test_parallel_timeout_is_not_retryable():
    import threading

    release = threading.Event()
    slow = FunctionAgent("slow", lambda t: (release.wait(5), "late")[1])
    try:
        [result] = Coordinator([slow]).run_parallel("t", timeout=0.05)
    finally:
        release.set()
    assert result.error is not None and result.error["code"] == "orchestration.timeout"
    assert result.error["retryable"] is False  # 原任务可能仍在后台跑:重试会并发出重复副作用
    assert AgentTimeoutError.retryable is False


# ---- CalcTool:统一成文档承诺的 ValueError;深度按真正的嵌套计 ------------------------------------------


@pytest.mark.parametrize("expr", ["10.0**400", "(" * 300 + "1" + ")" * 300, "1+", "1 +* 2"])
def test_calc_errors_are_value_errors(expr):
    with pytest.raises(ValueError):
        CalcTool().run(expr)


def test_calc_long_flat_chains_are_not_depth_errors():
    assert CalcTool().run("+".join(["1"] * 1_000)).output == "1000"
    assert CalcTool().run("*".join(["1"] * 500)).output == "1"


def test_calc_true_nesting_is_still_bounded():
    with pytest.raises(ValueError):
        CalcTool().run("1+(" * 150 + "1" + ")" * 150)


# ---- include_error_message=True 时异常消息截断 ----------------------------------------------------------


def test_included_error_message_is_truncated():
    @function_tool
    def explode(value: str) -> str:
        raise RuntimeError("x" * 10_000)

    seen: list[str] = []

    class Recording(ScriptedToolCallProvider):
        def chat(self, messages, *, tools=None):
            seen.extend(m["content"] for m in messages if m.get("role") == "tool")
            return super().chat(messages, tools=tools)

    agent = FunctionCallingAgent(
        "fc", Recording([("explode", {"value": "v"})]), [explode], include_error_message=True
    )
    agent.step("go")
    assert seen and len(seen[0]) < 600 and "RuntimeError" in seen[0]
