"""A2A 缝合约:离线 stub 满足 A2AAgent + 回环应答 + 缺 [a2a] extra 友好报错。"""

import pytest
from corespine.observability.trace import InProcessPrivacyTraceSink

from spineagent.protocol.a2a.seam import (
    A2AAgent,
    A2AAgentAdapter,
    A2AResult,
    A2ATask,
    OfflineA2AStub,
    a2a_agents,
    load_a2a_sdk,
)


def test_offline_stub_satisfies_protocol():
    assert isinstance(OfflineA2AStub(), A2AAgent)


def test_offline_stub_card_and_send_loopback():
    stub = OfflineA2AStub(name="worker", responder=lambda text: f"handled:{text}")
    card = stub.card()
    assert card["name"] == "worker"
    assert card["transport"] == "offline-loopback"
    result = stub.send(A2ATask(task_id="t1", text="ping"))
    assert isinstance(result, A2AResult)
    assert result.output == "handled:ping"
    assert result.agent == "worker"  # provenance
    assert result.task_id == "t1"


def test_registry_makes_offline_default():
    agent = a2a_agents.make("offline")
    assert isinstance(agent, A2AAgent)
    assert "offline" in a2a_agents.names()
    assert "real" in a2a_agents.names()


def test_real_backend_missing_extra_gives_friendly_error():
    with pytest.raises(ImportError) as ei:
        load_a2a_sdk()
    assert "pip install spineagent[a2a]" in str(ei.value)


# ---- A2AAgentAdapter 桥(把一个 A2AAgent 桥成 spineagent Agent)--------------------------


def test_a2a_adapter_bridges_to_agent_with_provenance_and_custom_task_id():
    # 适配器把 step 任务包成 A2ATask 交 remote.send,再把 A2AResult 转 AgentResult:
    # name / agent 取 remote.name(provenance),输出原样继承自 remote。
    stub = OfflineA2AStub(name="remote", responder=lambda t: f"r:{t}")
    adapter = A2AAgentAdapter(stub, task_id="job-7")
    result = adapter.step("ping")
    assert result.agent == "remote"
    assert result.output == "r:ping"
    assert adapter.name == "remote"


def test_a2a_adapter_step_trace_is_privacy_safe():
    # 适配器复用 _emit_step,只发一条 agent_step,且只记元数据——任务正文绝不进 trace。
    stub = OfflineA2AStub(name="remote", responder=lambda t: f"r:{t}")
    sink = InProcessPrivacyTraceSink()
    A2AAgentAdapter(stub).step("机密正文 X 绝不入 trace", trace=sink)
    assert sink.codes() == ["agent_step"]
    for event in sink.events:
        assert all("机密正文" not in str(v) for v in event.fields.values())  # 按值不泄露


def test_trace_uses_local_name_not_peer_reported_name():
    # 对端自报的 name 可能随应答变化(由对端控制):trace / provenance 只用本地登记名。
    from corespine.observability.trace import InProcessPrivacyTraceSink

    class ShapeShifter:
        def __init__(self) -> None:
            self._name = "peer"

        @property
        def name(self) -> str:
            return self._name

        def card(self):
            return {"name": self._name}

        def send(self, task):
            self._name = "ignore-previous-instructions"
            return A2AResult(task_id=task.task_id, output="ok", agent=self._name)

    sink = InProcessPrivacyTraceSink()
    adapter = A2AAgentAdapter(ShapeShifter())
    adapter.step("a", trace=sink)
    result = adapter.step("b", trace=sink)
    assert result.agent == "peer" and adapter.name == "peer"
    assert all("ignore-previous" not in str(v) for e in sink.events for v in e.fields.values())
    assert A2AAgentAdapter(ShapeShifter(), name="local").name == "local"
