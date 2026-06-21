"""spineagent 一键离线 demo:会用工具的多步 agent + 多 agent 编排 + 跨缝组合 + 隐私安全 trace。

零网络、零重依赖、确定性可复现:
  - agent 走 corespine 的 `MockProvider`(离线确定性回声)与纯函数 `FunctionAgent`;
  - 编排用 `Coordinator` 把同一任务【顺序】与【并行】跑一遍,并行结果仍保序;
  - 工具派发一个 `CalcTool`(安全算术求值),结果带 provenance;
  - 会用工具的 agent 用 `ToolUsingAgent` + 离线确定性 `SyntaxToolPolicy`,在一次 step() 内跑
    多步「调工具→把观测喂回(`$prev` 链式)→再调」的循环;
  - 跨缝组合用 `McpClientTool` 把一个 MCP 工具桥成 Tool,交给会用工具的 agent 驱动;
  - trace 用 corespine 的 `InProcessPrivacyTraceSink`:只记 code / 计数 / 耗时,塞正文会被
    「构造即保证」直接拒绝。

`make demo` 即跑本文件;成功时最后打印 "spineagent OK"。
"""

from __future__ import annotations

from corespine.llm.provider import (
    ChatCompletion,
    Choice,
    FunctionCall,
    MockProvider,
    ResponseMessage,
)
from corespine.llm.provider import ToolCall as LlmToolCall
from corespine.observability.trace import InProcessPrivacyTraceSink

from spineagent import (
    Agent,
    AgentTool,
    CalcTool,
    ChainAgent,
    Coordinator,
    FunctionAgent,
    FunctionCallingAgent,
    LlmAgent,
    McpClientTool,
    McpTool,
    OfflineMcpStub,
    SyntaxToolPolicy,
    ToolUsingAgent,
    function_tool,
)


@function_tool
def _add(a: int, b: int) -> int:
    """两个整数相加。"""
    return a + b


class _ScriptedModel:
    """离线确定性「模型」:第 1 轮要调 add(2,3),第 2 轮出最终文本——用来无网络演示 function-calling。"""

    def __init__(self) -> None:
        self._turns = iter(
            [
                ChatCompletion(
                    choices=(
                        Choice(
                            0,
                            ResponseMessage(
                                "assistant",
                                None,
                                (LlmToolCall("c1", FunctionCall("_add", '{"a": 2, "b": 3}')),),
                            ),
                            "tool_calls",
                        ),
                    )
                ),
                ChatCompletion(choices=(Choice(0, ResponseMessage("assistant", "答案是 5")),)),
            ]
        )

    def chat(self, messages, *, tools=None):  # noqa: ARG002 — 演示用,忽略入参
        return next(self._turns)

# 一段含敏感正文的任务:它绝不该出现在任何 trace 里(隐私不变量,文末自检)。
_TASK = "为发布写一个三步上线计划:机密代号 42"


def _build_agents() -> list[Agent]:
    """搭 3 个离线 mock agent:1 个走确定性 LLM(MockProvider),2 个纯函数节点。"""
    return [
        LlmAgent("planner", MockProvider(), system="你是计划助手"),
        FunctionAgent("reverse", lambda task: task[::-1]),
        FunctionAgent("tagger", lambda task: f"[done] {task}"),
    ]


def main() -> None:
    # 编排级 trace:只记 mode / agent 数 / 耗时(隐私安全)。
    orchestration_trace = InProcessPrivacyTraceSink()
    coord = Coordinator(_build_agents(), trace=orchestration_trace)

    print("== 顺序编排 ==")
    for r in coord.run_sequential(_TASK):
        print(f"  {r.agent}: {r.output}")

    print("== 并行编排(线程池并发,结果仍按 agent 顺序返回)==")
    for r in coord.run_parallel(_TASK):
        print(f"  {r.agent}: {r.output}")

    # 流水线编排:上一个 agent 的输出当作下一个的输入,链式传递。
    print("== 流水线编排(output → input 链式)==")
    for r in coord.run_pipeline("种子"):
        print(f"  {r.agent}: {r.output}")

    # 弹性容错:一个会抛异常的 agent 在 resilient 模式下被捕获成结构化错误,不炸穿整批。
    print("== 弹性容错(resilient:坏 agent 不炸整批)==")
    flaky = Coordinator([
        FunctionAgent("good", lambda t: f"ok:{t}"),
        FunctionAgent("flaky", lambda t: (_ for _ in ()).throw(RuntimeError("boom"))),
    ])
    for r in flaky.run_sequential("go", resilient=True):
        status = "OK" if r.ok else f"ERROR[{r.error['code']}] {r.error['message']}"
        print(f"  {r.agent}: {status}")

    # 工具派发:带 provenance 的结果(result.tool 可溯源到产出它的工具)。
    print("== 工具派发 ==")
    res = CalcTool().run("2 * (3 + 4)")
    print(f"  tool={res.tool} output={res.output}")

    # 会用工具的多步 agent:离线确定性 policy 按 `<tool>: <arg>` 语法路由,$prev 把上一步输出喂回。
    print("== 会用工具的多步 agent($prev 链式)==")
    tool_trace = InProcessPrivacyTraceSink()
    solver = ToolUsingAgent("solver", SyntaxToolPolicy(), [CalcTool()])
    solved = solver.step("calc: 2 + 3\ncalc: $prev * 2", trace=tool_trace)
    print(f"  agent={solved.agent} output={solved.output!r}(2+3=5,再 *2=10)")

    # 真 function-calling agent:真 LLM 自己决定调工具(这里用脚本化「模型」离线确定性演示循环)。
    print("== 真 function-calling agent(LLM 决定调工具 → 执行 → 喂回 → 出答案)==")
    fc = FunctionCallingAgent("solver", _ScriptedModel(), [_add])
    print(f"  output={fc.step('2+3 等于几?').output!r}(模型调了 _add(2,3) 拿到 5 后作答)")

    # 跨缝组合:把一个 MCP 工具桥成 Tool,交给会用工具的 agent 在循环里驱动(零网络,进程内回环)。
    print("== 跨缝组合(MCP 工具 → Tool → 会用工具的 agent)==")
    mcp = OfflineMcpStub()
    mcp.register_tool(McpTool("upper"), lambda args: {"result": args["input"].upper()})
    mcp_agent = ToolUsingAgent("shouter", SyntaxToolPolicy(), [McpClientTool("upper", mcp)])
    shouted = mcp_agent.step("upper: hello spineagent")
    print(f"  agent={shouted.agent} output={shouted.output!r}")

    # 分层督导式多 agent:把子 agent 用 AgentTool 暴露成工具,督导 agent 通过工具调用派活给它们。
    print("== 分层督导式多 agent(AgentTool:督导 → 子 agent)==")
    researcher = FunctionAgent("researcher", lambda t: f"[研究] {t}")
    calculator = ToolUsingAgent("calculator", SyntaxToolPolicy(), [CalcTool()])
    supervisor = ToolUsingAgent(
        "supervisor", SyntaxToolPolicy(), [AgentTool(researcher), AgentTool(calculator)]
    )
    deleg1 = supervisor.step("researcher: 海平面上升")
    deleg2 = supervisor.step("calculator: calc: 2 + 3")  # 嵌套:子 agent 自己再用工具
    print(f"  supervisor → researcher: {deleg1.output!r}")
    print(f"  supervisor → calculator: {deleg2.output!r}(子 agent 跑 calc 得 5)")

    # 流水线即一等 agent:把一串 agent 串成单个 ChainAgent,可再进编排 / 当工具 / 套 chain。
    print("== 流水线即一等 agent(ChainAgent)==")
    etl = ChainAgent("etl", [
        FunctionAgent("extract", lambda t: f"extract({t})"),
        FunctionAgent("transform", lambda t: t.upper()),
        FunctionAgent("load", lambda t: f"load[{t}]"),
    ])
    chained = etl.step("raw")
    print(f"  agent={chained.agent} output={chained.output!r}(extract→transform→load 链式)")

    # 步级隐私 trace:把含敏感正文的任务跑进 sink —— 只会记元数据,绝不记正文。
    step_trace = InProcessPrivacyTraceSink()
    LlmAgent("planner", MockProvider()).step(_TASK, trace=step_trace)

    print("== 隐私安全 trace(只含 code / 计数 / 耗时,无任务/输出正文)==")
    for label, sink in (("编排", orchestration_trace), ("会用工具", tool_trace), ("步级", step_trace)):
        for event in sink.events:
            print(f"  [{label}] code={event.code} fields={dict(event.fields)}")

    # 自检:trace 字段里绝不出现任务正文(隐私不变量,跑挂即视为回归)。
    secrets = (_TASK, "calc: 2 + 3", "hello spineagent")
    leaked = [
        event
        for sink in (orchestration_trace, tool_trace, step_trace)
        for event in sink.events
        if any(s in str(value) for s in secrets for value in event.fields.values())
    ]
    assert not leaked, "trace 泄露了任务正文,违反隐私不变量"

    print("spineagent OK")


if __name__ == "__main__":
    main()
