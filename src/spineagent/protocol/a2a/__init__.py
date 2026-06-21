"""spineagent.protocol.a2a —— A2A 缝:A2AAgent 协议 + 离线回环 stub。"""

from spineagent.protocol.a2a.seam import (
    A2AAgent,
    A2AResult,
    A2ATask,
    OfflineA2AStub,
    a2a_agents,
    load_a2a_sdk,
)

__all__ = [
    "A2AAgent",
    "A2ATask",
    "A2AResult",
    "OfflineA2AStub",
    "a2a_agents",
    "load_a2a_sdk",
]
