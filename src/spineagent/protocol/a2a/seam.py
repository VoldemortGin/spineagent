"""A2A(Agent-to-Agent)缝:A2AAgent 协议 + 离线回环 stub 默认。

家族缝的元模式(同 MCP 缝):Protocol + 离线确定性默认 + Registry 工厂 + 真实后端经可选
extra 延迟 import。默认路径【零网络、零重依赖】:OfflineA2AStub 在进程内回环——给出 agent
card(能力描述)并应答一条任务,让跨 agent 协作在离线 / 测试下也能端到端跑。

真实 A2A SDK(`a2a-sdk`,import 名 `a2a`)仅在选用时,经 [a2a] extra 由 corespine
.lazy_extra_import 延迟 import;未装时给「pip install spineagent[a2a]」友好报错。本模块
顶层【绝不】import 真实 SDK——import spineagent 不该拉入任何网络 SDK。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from corespine.errors import SeamError
from corespine.observability.trace import TraceSink
from corespine.seam.registry import Registry, lazy_extra_import

from spineagent.agent.agent import AgentResult, _emit_step

# 真实 A2A SDK 的 import 名(装了 spineagent[a2a] 才有);默认离线路径绝不 import 它。
_A2A_SDK_MODULE = "a2a"


@dataclass(frozen=True)
class A2ATask:
    """一条跨 agent 任务:task_id + 文本(协议载荷本身,非 trace)。"""

    task_id: str
    text: str


@dataclass(frozen=True)
class A2AResult:
    """跨 agent 任务结果:task_id + 产出 + 来源 agent 名(provenance)。"""

    task_id: str
    output: str
    agent: str


@runtime_checkable
class A2AAgent(Protocol):
    """A2A 协议:有名字;能给出 agent card(能力描述);能接收并应答一条任务。"""

    name: str

    def card(self) -> dict[str, Any]: ...

    def send(self, task: A2ATask) -> A2AResult: ...


class OfflineA2AStub:
    """离线回环 A2A:把一个本地应答函数暴露为 A2AAgent,零网络(默认 / 测试用)。"""

    def __init__(
        self,
        *,
        name: str = "offline-a2a",
        responder: Callable[[str], str] | None = None,
    ) -> None:
        self._name = name
        self._responder = responder or (lambda text: f"echo:{text}")

    @property
    def name(self) -> str:
        return self._name

    def card(self) -> dict[str, Any]:
        return {"name": self._name, "transport": "offline-loopback", "skills": ["echo"]}

    def send(self, task: A2ATask) -> A2AResult:
        return A2AResult(
            task_id=task.task_id, output=self._responder(task.text), agent=self._name
        )


class A2AAgentAdapter:
    """跨缝适配器:把一个 A2AAgent 桥成 spineagent Agent(实现 Agent 协议)。

    让一个远端 / 进程内的 A2A agent 能像本地 agent 一样进 Coordinator 顺序 / 并行编排:把 step
    的任务包成一条 A2ATask 交给 remote.send,再把 A2AResult 转成 AgentResult。name 取 remote.name
    以满足「结果可溯源到产出它的 agent」不变量;trace 复用本包同款 _emit_step,只记元数据。

    透明桥:输出原样继承自 remote(与 LlmAgent / FunctionAgent 透传 provider / 函数输出一致)。
    故「步产出非空」这条 Agent 不变量当且仅当 remote 自身产出非空时成立——本适配器不伪造、不
    篡改 remote 的应答;空应答属 remote 违约,由 remote 侧负责,本壳不在此兜底(rule of three)。
    """

    def __init__(self, remote: A2AAgent, *, task_id: str = "task") -> None:
        self._remote = remote
        self._task_id = task_id

    @property
    def name(self) -> str:
        return self._remote.name

    def step(self, task: str, *, trace: TraceSink | None = None) -> AgentResult:
        reply = self._remote.send(A2ATask(task_id=self._task_id, text=task))
        result = AgentResult(agent=self._remote.name, output=reply.output)
        _emit_step(trace, self._remote.name, task, result)
        return result


def load_a2a_sdk() -> Any:
    """延迟 import 真实 A2A SDK;未装 [a2a] extra 时给「pip install spineagent[a2a]」友好报错。"""
    return lazy_extra_import(_A2A_SDK_MODULE, pkg="spineagent", extra="a2a")


def _make_real_agent(**kwargs: Any) -> A2AAgent:
    # 缺 [a2a] extra -> 友好 ImportError(离线默认路径永远不会走到这)。
    sdk = load_a2a_sdk()
    # 装了 extra 但适配器尚未接入:家族统一 SeamError(「缝槽存在但真实实现未接入」)。
    raise SeamError(
        f"真实 A2A 适配器留待装了 spineagent[a2a] 的使用者按 {sdk.__name__!r} 接入;"
        "本壳只提供缝 + 离线 stub。"
    )


# 缝注册表:一个 spec 选实现(默认 offline 离线 stub;real 走延迟 import 的真实 SDK)。
a2a_agents: Registry[A2AAgent] = Registry("a2a_agent")
a2a_agents.register("offline", lambda **kw: OfflineA2AStub(**kw))
a2a_agents.register("real", _make_real_agent)
