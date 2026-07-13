"""预置 deep-research agent:纯组合既有原语,不新造机制。

DeepResearchAgent 把「分解 → 并行检索 → 综合」这条经典深研流水线,用【已有构件】装配起来:
  - 分解:一个可注入的 planner(task -> 子查询列表);离线默认 default_planner 做结构化切分
          (按行 / 分号),真实语义分解由调用方注入(如一个走 LLM 的 planner);
  - 并行检索:每个子查询包成一个 FunctionAgent(闭包绑定该子查询 + 共享的检索 FunctionCallingAgent),
          交给既有 Coordinator.run_parallel 扇出并【保序】收集——零新编排逻辑;
  - 综合:一个 LlmAgent 把并行发现拼成 prompt、走 provider 产出最终答案。

它本身实现 Agent 协议(name + step),故可再进 Coordinator / 被 AgentTool 当工具 / 套 ChainAgent。
离线确定性默认:provider 缺省 MockProvider、tools 缺省空——离线端到端可跑、可复现;真实检索效果
靠调用方注入 provider + 检索 tools(如把 ragspine RAG 桥成的 FunctionTool)。

隐私:只发 deep_research 编排级 trace(子查询数 / 发现数 / 输出长度);内部 Coordinator / synthesizer
各自发自己的隐私安全 trace,全程只记计数,绝不记任务 / 子查询 / 发现正文。
"""

from collections.abc import Callable, Iterable

from corespine.llm.provider import LLMProvider, MockProvider
from corespine.observability.trace import TraceSink

from spineagent.agent.agent import AgentResult, FunctionAgent, LlmAgent
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.orchestration.coordinator import Coordinator
from spineagent.tools.function_tool import FunctionTool

# 分解切分的分隔符(离线默认 planner 用):换行 + 中英文分号。
_SPLIT_CHARS = ("\n", ";", "；")


def default_planner(task: str) -> list[str]:
    """离线确定性默认分解:按换行 / 分号做结构化切分,去空白、去空行;无可切分则整条作单一子查询。

    刻意【结构化而非语义】:MockProvider 只回声、不可能语义分解,故离线默认诚实地只做确定性结构
    切分;真实语义分解(把一个问题拆成多个检索角度)由调用方注入一个走 LLM 的 planner。
    """
    parts = [task]
    for sep in _SPLIT_CHARS:
        parts = [seg for chunk in parts for seg in chunk.split(sep)]
    subqueries = [p.strip() for p in parts if p.strip()]
    return subqueries or [task.strip() or task]


class DeepResearchAgent:
    """预置深研 agent(纯组合:planner 分解 + Coordinator 并行检索 + LlmAgent 综合),实现 Agent 协议。"""

    def __init__(
        self,
        name: str = "deep_research",
        *,
        provider: LLMProvider | None = None,
        tools: Iterable[FunctionTool] = (),
        planner: Callable[[str], list[str]] | None = None,
        max_subqueries: int = 5,
        retriever_system: str = "",
        synthesis_system: str = "",
    ) -> None:
        self._name = name
        self._provider = provider if provider is not None else MockProvider()
        self._tools = list(tools)
        self._planner = planner if planner is not None else default_planner
        self._max_subqueries = max_subqueries
        self._retriever_system = retriever_system
        self._synthesis_system = synthesis_system

    @property
    def name(self) -> str:
        return self._name

    def step(self, task: str, *, trace: TraceSink | None = None) -> AgentResult:
        # 1) 分解:子查询列表(封顶 max_subqueries;planner 返空则回落整条任务)。
        subqueries = (self._planner(task) or [task])[: self._max_subqueries]

        # 2) 并行检索:共享一个检索 FunctionCallingAgent(线程安全:step 内建局部状态),每个子查询
        #    包成一个绑定该子查询的 FunctionAgent,交给 Coordinator.run_parallel 扇出并保序收集。
        retriever = FunctionCallingAgent(
            f"{self._name}.retriever", self._provider, self._tools, system=self._retriever_system
        )
        retrieval_agents = [
            FunctionAgent(f"{self._name}.retrieve.{i}", _bind_query(retriever, sq))
            for i, sq in enumerate(subqueries)
        ]
        findings = Coordinator(retrieval_agents, trace=trace).run_parallel("", resilient=True)

        # 3) 综合:把并行发现拼成 prompt,走 provider 产出最终答案。
        digest = "\n\n".join(f"[发现 {i + 1}] {f.output}" for i, f in enumerate(findings))
        synthesis_prompt = f"原始研究问题:{task}\n\n基于以下并行检索到的发现,综合成一份连贯、无编造的回答:\n\n{digest}"
        synthesizer = LlmAgent(
            f"{self._name}.synthesize", self._provider, system=self._synthesis_system
        )
        final = synthesizer.step(synthesis_prompt, trace=trace)

        _emit(trace, self._name, len(subqueries), len(findings), final.output)
        # provenance 重盖为本 agent(对外它是产出者;子 agent 名是内部细节,与 ChainAgent / AgentTool 同理)。
        return AgentResult(agent=self._name, output=final.output, usage=final.usage)


def _bind_query(retriever: FunctionCallingAgent, subquery: str) -> Callable[[str], str]:
    """把「共享检索 agent + 某个子查询」绑成一个 (task->text) 纯函数(供 FunctionAgent 包装)。

    忽略传入 task(并行时 Coordinator 对所有 agent 传同一空任务),对绑定的子查询跑检索、取其输出。
    """
    return lambda _task: retriever.step(subquery).output


def _emit(trace: TraceSink | None, name: str, subqueries: int, findings: int, output: str) -> None:
    """记一条隐私安全的编排级 trace:agent 名 / 子查询数 / 发现数 / 输出长度,绝不记正文。"""
    if trace is None:
        return
    trace.emit(
        "deep_research",
        agent=name,
        subqueries=subqueries,
        findings=findings,
        output_chars=len(output),
    )
