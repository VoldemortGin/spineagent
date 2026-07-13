"""sandbox 缝:隔离执行 skill / tool 代码。

Protocol + 离线确定性默认(InProcessSandbox 受限白名单求值器)+ Registry 工厂 + 参数化
conformance。真实硬隔离后端(subprocess / container)走 [sandbox] extra 延迟 import。
"""

from spineagent.sandbox.seam import (
    DEFAULT_LIMITS,
    InProcessSandbox,
    Limits,
    ResourceUsage,
    Sandbox,
    SandboxResult,
    load_container_sdk,
    sandboxes,
)

__all__ = [
    "Sandbox",
    "SandboxResult",
    "ResourceUsage",
    "Limits",
    "DEFAULT_LIMITS",
    "InProcessSandbox",
    "sandboxes",
    "load_container_sdk",
]
