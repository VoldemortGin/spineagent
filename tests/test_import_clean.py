"""import-clean 合约:import agentspine 绝不拉入任何网络 SDK(在干净子进程里断言)。

默认路径必须零网络、零重依赖:协议缝的真实 SDK 只在显式选用时经可选 extra 延迟 import。
用一个全新解释器子进程 import agentspine,再扫 sys.modules,确认没有任何网络 SDK 被带入。
"""

import subprocess
import sys

# 默认路径下绝不该出现的网络 / 服务 SDK(含 MCP / A2A 真实后端及常见传输栈)。
_NETWORK_SDKS = [
    "mcp",
    "a2a",
    "httpx",
    "httpcore",
    "anthropic",
    "openai",
    "cohere",
    "boto3",
    "google",
    "requests",
    "aiohttp",
    "grpc",
    "websockets",
    "starlette",
    "fastapi",
    "uvicorn",
]


def test_import_agentspine_pulls_no_network_sdk():
    script = (
        "import sys, agentspine; "
        f"leaked = [m for m in {_NETWORK_SDKS!r} if m in sys.modules]; "
        "print(','.join(leaked))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    leaked = proc.stdout.strip()
    assert leaked == "", f"import agentspine 不该拉入网络 SDK,却拉入了:{leaked}"
