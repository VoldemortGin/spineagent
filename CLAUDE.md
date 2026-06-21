# CLAUDE.md — agentspine(宪章)

Spine 家族的 AI / 人类协作契约。先读家族 `../README.md` 与
`../docs/adr/0001-spine-family-boundaries-and-dependency-direction.md`,本文件是 agentspine 的操作指南。

## 这是什么

**agentspine —— 通用多 agent 协作框架**(ADR 0001 D1)。agent / tool / 编排 + MCP / A2A 等
agent 协议缝。它是家族里【演进最快】的成员,刻意单开独立包,**不**拖累薄核与兄弟包。它依赖薄核
`corespine`,复用其缝元模式与 observability / config 形状,但**不**含任何 RAG 概念。

## 宪章(不可违背)

- **离线优先、import-clean、零重依赖默认路径。** 核心默认路径只用标准库 + `corespine`;真实协议
  SDK(MCP / A2A 等)经**可选 extra 延迟 import**(`corespine.lazy_extra_import`),缺 extra 时给
  「pip install agentspine[<extra>]」友好报错。**import agentspine 绝不拉入任何网络 SDK**
  (有 `tests/test_import_clean.py` 把这条钉死)。
- **每条缝长一个样**(家族统一元模式):**Protocol + 离线确定性默认 + Registry 工厂 + 参数化
  conformance**。core 只 import Protocol,绝不 import SDK。
- **机制借 corespine,保证本包自绑**(ADR 0001 D6)。corespine 只给 conformance 基座;具体不变量
  (agent 步 provenance、步级 trace 隐私安全、tool 结果 provenance)由 `agentspine/conformance.py`
  绑定,并用参数化 conformance 测试钉死。
- **不在包层面依赖 ragspine。** 可在【运行时】把 ragspine 当一个 Tool / MCP server 组合调用
  (ADR 0001 D4b),那是松耦合的可选组合,方向只能 agentspine→ragspine,绝不反向,也绝不写进
  `dependencies`。
- **隐私 trace。** 任何步级 / 编排级 trace 只记 code / 计数 / 耗时,绝不记任务或输出正文。

## 模块地图(按文件夹定位)

```
src/agentspine/
  agent/agent.py            Agent 协议 + LlmAgent(走 corespine LLMProvider)/ FunctionAgent(纯函数)
  agent/policy.py           ToolPolicy 缝:协议 + 离线确定性默认 SyntaxToolPolicy(`<tool>: <arg>` 语法路由,不假装 LLM 推理)+ tool_policies Registry
  agent/tool_using.py       ToolUsingAgent:单次 step() 内多步「调工具→观测喂回($prev 链式)→再调」循环,带 max_steps 守卫;实现 Agent 协议
  agent/as_tool.py          AgentTool:把 Agent 桥成 Tool(分层 / 督导式多 agent:督导 agent 通过工具调用派活给子 agent,可嵌套)
  tools/tool.py             Tool 协议 + EchoTool / CalcTool + tool_registry(spec 选工具 + entry-point 第三方工具发现,group corespine.tool);注:运行时可把 ragspine RAG 插为 Tool
  orchestration/coordinator.py  Coordinator:顺序 / 并行 / 流水线(output→input)跑多 agent、保序收集;弹性容错 resilient 把异常归一为 AgentResult.error,坏 agent 不炸整批
  orchestration/chain.py        ChainAgent:把一串 agent 串成单个 Agent(流水线即一等可组合单元:可进 Coordinator / 当工具 / 套 chain)
  protocol/mcp/seam.py      McpClient / McpServer 协议 + OfflineMcpStub(离线回环)+ McpClientTool(MCP 工具→Tool)+ 延迟真实 SDK
  protocol/a2a/seam.py      A2AAgent 协议 + OfflineA2AStub(离线回环)+ A2AAgentAdapter(A2A→Agent)+ 延迟真实 SDK
  conformance.py            本包绑定的不变量包(AGENT_INVARIANTS / TOOL_INVARIANTS / POLICY_INVARIANTS)
```

## 跑(始终从包根)

```bash
uv venv .venv
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e ../corespine   # 本地兄弟包
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # 期望 GREEN
.venv/bin/python -c "import agentspine"  # 期望 import-clean(无网络 SDK)
```

## 约定

- Python **3.10+** 类型注解;import 顺序 **stdlib > 三方 > 本地**;简体中文 docstring/注释,匹配家族风格。
- **TDD**——测试即规格;**最小改动**——只改需求要求的部分。
- **深层、按领域分组**的布局:文件路径先定位职责,再读文件名。
