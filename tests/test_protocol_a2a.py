"""A2A 缝合约:离线 stub 满足 A2AAgent + 回环应答 + 缺 [a2a] extra 友好报错。"""

import pytest

from spineagent.protocol.a2a.seam import (
    A2AAgent,
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
