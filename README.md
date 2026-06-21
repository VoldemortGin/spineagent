# agentspine

Spine 家族的**通用多 agent 协作框架**(见 [ADR 0001](../docs/adr/0001-spine-family-boundaries-and-dependency-direction.md))。
agent / tool / 编排 + **MCP / A2A** 等 agent 协议缝。依赖薄核 `corespine`,复用其缝元模式与
observability / config 形状;**默认路径离线可跑、import-clean、零网络 SDK**。

> 通用 ≠ 地基。真正的核是更薄的 `corespine`,agentspine 是它的兄弟消费者,**不**含任何 RAG 概念。
> 详见 [`CLAUDE.md`](CLAUDE.md) 宪章。

## 缝的元模式(家族统一)

每条缝都长一个样,核心 import 零 SDK、离线可跑:

**Protocol + 离线确定性默认 + `Registry` 工厂 + 参数化 conformance**

## 里面有什么

| 模块 | 原语 |
|---|---|
| `agent/agent.py` | `Agent` 协议 + `LlmAgent`(走 corespine `LLMProvider`,离线用 `MockProvider`)/ `FunctionAgent`(纯函数节点);步级 trace 只记元数据 |
| `llm/provider.py` 等 | 真实 LLM provider 适配器(挂在 corespine `LLMProvider` 缝后面;**对外统一 OpenAI chat-completions 形状**):`OpenAICompatProvider`(`openai` SDK + `base_url`,一个吃下 OpenAI/Azure/Together/Groq/DeepSeek/Mistral/xAI/Qwen/Moonshot/Ollama/vLLM/OpenRouter/LiteLLM… 全部 OpenAI 兼容端点)+ 非 OpenAI 原生适配器把 native 转成 OpenAI 形状:`AnthropicProvider`(默认 `claude-opus-4-8`)/ `CohereProvider` / `GeminiProvider` / `BedrockConverseProvider`。`llm_providers` Registry(mock/openai/anthropic/cohere/gemini/bedrock)。各走可选 extra 延迟 import,离线默认仍是 `MockProvider`,各用原生 SDK 不 shim |
| `agent/policy.py` | `ToolPolicy` 协议 + 离线确定性默认 `SyntaxToolPolicy`(按 `<tool>: <arg>` 语法 + 工具名集合确定性路由,**不假装 LLM 推理**)+ `tool_policies` Registry(`llm` 位留真实推理式接入) |
| `agent/tool_using.py` | `ToolUsingAgent`:在单次 `step()` 内跑「决策→调工具→把观测喂回(`$prev` 链式)→再决策」的多步循环,带 `max_steps` 守卫;实现 `Agent` 协议故可直接进 `Coordinator` |
| `agent/as_tool.py` | `AgentTool`:把一个 `Agent` 桥成 `Tool`,让督导 agent 通过工具调用把子任务派给专精子 agent(**分层 / 督导式多 agent**,可层层嵌套) |
| `tools/tool.py` | `Tool` 协议 + `EchoTool` / `CalcTool`(安全算术求值);结果带 provenance。`tool_registry`:spec 选工具 + **entry-point 第三方工具自动发现**(group `corespine.tool`)。**运行时可把 ragspine RAG 插为一个 Tool**(见下) |
| `orchestration/coordinator.py` | `Coordinator`:把多个 agent **顺序 / 并行 / 流水线**(output→input 链式)跑、保序收集 `AgentResult`;**弹性容错**(`resilient=True`)把单 agent 异常归一为家族错误 dict 塞进 `AgentResult.error`、一个坏 agent 不炸整批 |
| `orchestration/chain.py` | `ChainAgent`:把一串 agent 串成**单个 `Agent`**(流水线即一等可组合单元),可再进 `Coordinator` / 被 `AgentTool` 当工具 / 套 chain |
| `protocol/mcp/seam.py` | `McpClient` / `McpServer` 协议 + `OfflineMcpStub`(进程内回环)+ **`McpClientTool`(把 MCP 工具桥成 `Tool`)** + 真实 SDK 经 `[mcp]` extra 延迟 import |
| `protocol/a2a/seam.py` | `A2AAgent` 协议 + `OfflineA2AStub`(进程内回环)+ **`A2AAgentAdapter`(把 A2A agent 桥成 `Agent`)** + 真实 `a2a-sdk` 经 `[a2a]` extra 延迟 import |
| `conformance.py` | 本包绑定的不变量:`AGENT_INVARIANTS`(步产出 / provenance / 隐私 trace)、`TOOL_INVARIANTS`(结果 provenance)、`POLICY_INVARIANTS`(决策形状 / 不幻觉工具 / 可终止 / 纯函数) |

## 运行时组合 ragspine(ADR 0001 D4b)

agentspine **不**在包层面依赖 ragspine。但可在**运行时**把 ragspine 的 RAG 检索包成一个实现了
`Tool`(或 MCP server)协议的适配器,插给某个 agent 调用——松耦合、可选,方向只能 agentspine→ragspine。
本包 `dependencies` 永远不含 ragspine,也绝不在默认路径 import 它。

第三方工具(含 ragspine RAG)还可经 entry-point 在 `corespine.tool` group 下注册工具工厂,即被
`tool_registry.make / names` 自动发现、零改本包代码组合进 agent;而它们仍须过 `TOOL_INVARIANTS`
conformance 才算数——「敢放手让第三方填广度,却让脊柱不变量烂不掉」。

## 本地开发(始终从包根)

```bash
uv venv .venv
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e ../corespine
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python -c "import agentspine"
```

## 30 秒上手

```python
from corespine import MockProvider, InProcessPrivacyTraceSink
from agentspine import LlmAgent, FunctionAgent, Coordinator, EchoTool, OfflineMcpStub
from agentspine.protocol.mcp.seam import McpTool

# 一个离线 agent:走 corespine 的确定性 MockProvider,跑单步
agent = LlmAgent("planner", MockProvider())
print(agent.step("列个计划").output)            # 确定性、可复现

# 多 agent 编排:顺序 / 并行跑同一任务,保序收集
coord = Coordinator([FunctionAgent("a", lambda t: f"a:{t}"),
                     FunctionAgent("b", lambda t: f"b:{t}")])
print([r.output for r in coord.run_parallel("go")])   # ['a:go', 'b:go']
print([r.output for r in coord.run_pipeline("go")])   # ['a:go', 'b:a:go'](链式:上一个输出喂下一个)

# 弹性容错:坏 agent 不炸整批,异常归一为结构化 error,批次照常跑完
flaky = Coordinator([FunctionAgent("ok", lambda t: t),
                     FunctionAgent("bad", lambda t: 1 / 0)])
print([(r.agent, r.ok) for r in flaky.run_sequential("go", resilient=True)])  # [('ok', True), ('bad', False)]

# 工具:带 provenance 的结果
print(EchoTool().run("hi").tool)                # 'echo'

# 工具缝注册表:按 spec 选工具(大小写/留白不敏感),第三方还能经 entry-point 自动发现
from agentspine import tool_registry
print(tool_registry.make("calc").run("6/2").output)   # '3'
print("calc" in tool_registry.names())                # True

# 会用工具的多步 agent:离线确定性 policy 按 `<tool>: <arg>` 语法路由,$prev 把上一步输出喂回
from agentspine import ToolUsingAgent, SyntaxToolPolicy, CalcTool
solver = ToolUsingAgent("solver", SyntaxToolPolicy(), [CalcTool()])
print(solver.step("calc: 2 + 3\ncalc: $prev * 2").output)   # '10'(2+3=5,再 *2=10)

# 分层督导式多 agent:把子 agent 用 AgentTool 暴露成工具,督导 agent 通过工具调用派活给它
from agentspine import AgentTool
calculator = ToolUsingAgent("calculator", SyntaxToolPolicy(), [CalcTool()])
supervisor = ToolUsingAgent("supervisor", SyntaxToolPolicy(), [AgentTool(calculator)])
print(supervisor.step("calculator: calc: 2+3").output)      # '5'(督导派给子 agent,子 agent 再用工具)

# MCP 离线回环:注册 + 调用,零网络
stub = OfflineMcpStub()
stub.register_tool(McpTool("upper"), lambda a: {"result": a["s"].upper()})
print(stub.call_tool("upper", {"s": "hi"}))     # {'result': 'HI'}

# 跨缝组合:把上面那个 MCP 工具桥成 Tool,交给会用工具的 agent 在循环里驱动(零网络)
from agentspine import McpClientTool
shouter = ToolUsingAgent("shouter", SyntaxToolPolicy(), [McpClientTool("upper", stub, arg_key="s")])
print(shouter.step("upper: hi").output)         # 'HI'

# 隐私 trace:步级只记元数据;塞正文会被 corespine 的 sink 直接拒绝
sink = InProcessPrivacyTraceSink()
agent.step("敏感任务", trace=sink)               # 只记 agent 名 / 长度 / token 数
```

## 换上真实模型(可选 extra)

**对外统一 OpenAI chat-completions 形状**(LiteLLM 模式):无论后端是谁,`chat(messages, tools)`
都回 OpenAI 形状的 `ChatCompletion`(`choices[0].message.content/.tool_calls`、`finish_reason`、
`usage.prompt_tokens`…)。`LlmAgent` 全程只认 corespine 的 `LLMProvider` 协议,把 `MockProvider`
换成真实适配器即可,其余代码(agent / 编排 / 工具循环)一行不改:

```bash
pip install "agentspine[openai]"      # OpenAI 及一切「OpenAI 兼容」端点
pip install "agentspine[anthropic]"   # 或 [cohere] / [gemini] / [bedrock]
```

```python
from agentspine import OpenAICompatProvider, AnthropicProvider, GeminiProvider, LlmAgent

# 一个适配器吃下所有 OpenAI 兼容端点:OpenAI / Azure / Together / Groq / DeepSeek / Mistral /
# xAI / 通义 Qwen / Moonshot / Ollama / vLLM / OpenRouter / LiteLLM …(换 base_url + model 即可)
gpt = LlmAgent("gpt", OpenAICompatProvider("gpt-4o"))
local = LlmAgent("local", OpenAICompatProvider("llama3", base_url="http://localhost:11434/v1"))

# 非 OpenAI 原生模型:原生适配器在内部转成 OpenAI 形状,用户无感
claude = LlmAgent("claude", AnthropicProvider())                 # 默认 claude-opus-4-8
gemini = LlmAgent("gemini", GeminiProvider(model="gemini-2.5-flash"))
```

> 覆盖:**OpenAI 兼容生态(约 85% 主流市场)走 `OpenAICompatProvider` 一把梭**;真正非 OpenAI 形状的
> Anthropic / Cohere / Gemini / Bedrock 各有原生适配器,把 native 响应转成 OpenAI `ChatCompletion`
> (绝不把它们套进 OpenAI 形状 = 不 shim)。默认离线路径仍是 `MockProvider`,`import agentspine`
> 永远零网络 SDK(真实 SDK 仅在选用对应 extra 时延迟 import)。reasoning / citations 等扩展本期丢弃。
