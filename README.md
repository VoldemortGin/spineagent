# spineagent

Spine 家族的**通用多 agent 协作框架**(见 [ADR 0001](../docs/adr/0001-spine-family-boundaries-and-dependency-direction.md))。
agent / tool / 编排 + **MCP / A2A** 等 agent 协议缝。依赖薄核 `corespine`,复用其缝元模式与
observability / config 形状;**默认路径离线可跑、import-clean、零网络 SDK**。

## Spine 家族 / Spine family

本仓库是 Spine 家族的成员之一（角色：L1 引擎）。家族全部成员、分层、依赖方向、依赖形式与当前差距见 [`docs/spine-family.md`](docs/spine-family.md)；该文件在每个家族仓库中的副本内容相同，真源在家族根目录 `~/startup/spine/docs/spine-family.md`，用根目录 `make family-doc-sync` 同步。

> 通用 ≠ 地基。真正的核是更薄的 `corespine`,spineagent 是它的兄弟消费者,**不**含任何 RAG 概念。
> 详见 [`CLAUDE.md`](CLAUDE.md) 宪章。
>
> **🤖 给 AI / LLM:** 用本库前先读 [`llms.txt`](llms.txt)(精简索引)与 [`docs/llms/`](docs/llms/)(完整 API / recipes / 陷阱);`pip install` 后这些文档随包位于 `site-packages/spineagent/_llms/`。

## 缝的元模式(家族统一)

每条缝都长一个样,核心 import 零 SDK、离线可跑:

**Protocol + 离线确定性默认 + `Registry` 工厂 + 参数化 conformance**

## 里面有什么

| 模块 | 原语 |
|---|---|
| `agent/agent.py` | `Agent` 协议 + `LlmAgent`(走 corespine `LLMProvider`,离线用 `MockProvider`)/ `FunctionAgent`(纯函数节点);步级 trace 只记元数据 |
| `llm/provider.py` 等 | 真实 LLM provider 适配器(挂在 corespine `LLMProvider` 缝后面;**对外统一 OpenAI chat-completions 形状**):`OpenAICompatProvider`(`openai` SDK + `base_url`,一个吃下 OpenAI/Azure/Together/Groq/DeepSeek/Mistral/xAI/Qwen/Moonshot/Ollama/vLLM/OpenRouter/LiteLLM… 全部 OpenAI 兼容端点)+ 非 OpenAI 原生适配器把 native 转成 OpenAI 形状:`AnthropicProvider`(默认 `claude-opus-4-8`)/ `CohereProvider` / `GeminiProvider` / `BedrockConverseProvider`。`llm_providers` Registry(mock/openai/anthropic/cohere/gemini/bedrock/**failover**)。各走可选 extra 延迟 import,离线默认仍是 `MockProvider`,各用原生 SDK 不 shim。**`FailoverProvider`**(组合式容错):包裹一组下游做轮询分摊 + 撞可重试错冷却 + 全冷却时强制跨 provider 回退,全失败抛脱敏聚合错(绝不含凭据);只回退可重试错、绝不吞逻辑错;适配器按状态码把 400 / 413 / 422 判为 `NonRetryableProviderError`,failover 对它不回退、不冷却;仅全下游支持流式时才诚实声明流式 |
| `agent/policy.py` | `ToolPolicy` 协议 + 离线确定性默认 `SyntaxToolPolicy`(按 `<tool>: <arg>` 语法 + 工具名集合确定性路由,**不假装 LLM 推理**;**只解析调用方给的指令,不解析数据段**,见 `agent/trust.py`)+ `tool_policies` Registry(`llm` 位是**占位、尚未实现**,`make("llm")` 必抛 `SeamError`) |
| `agent/trust.py` | 信任边界(ADR 0003):`TaskText`(带不可信数据区间的 str 子类)/ `untrusted` / `compose`。pipeline 上游输出、附件、工具结果、MCP / A2A 返回在源头标为数据,指令语法解析器不执行其中的 `<tool>: <arg>`;旧行为需 `SyntaxToolPolicy(parse_untrusted=True)` |
| `agent/middleware.py` | `Middleware` 协议(`before_step` / `after_step`)+ `MiddlewareAgent` 洋葱链(`StepContext.cleanups` 无论成败都执行)+ 内置 `TokenUsage` / `Summary` / `DynamicTool` / `Attachment` + `middlewares` Registry |
| `agent/approval.py` | 审批门 / Wait 缝(ADR 0001 / 0002):`ApprovalGate` 协议 + `AutoApprovalGate` / `ManualApprovalGate`(一次性 resume token;请求表有界、满了拒绝新请求;`pending()` 里的请求带完整参数 `arguments()` 与列表用 preview)+ `ApprovalMiddleware`。审批是**工具调用点上的强制闸**:每次真实工具调用前按「工具名 + 规范化参数 + 作用域」review,未批准不执行、门故障 fail-closed、受审批工具名笔误 fail-closed;批准**缺省一次性消费**、绑定**显式**作用域(会产生待审请求的门缺作用域即报配置错误);挂起后重跑是**至少一次**语义(多个受审批调用分布在多轮时按 2^N 重跑,重复的请求带 `prior_executions`),此类流程**必须**用 `on_approval="feed_back"`;`require_approval(tool, gate)` 把闸绑进工具对象本身(安全场景首选) |
| `agent/artifact.py` | artifact 缝:`Artifact` / `ArtifactRef` + `ArtifactSink`(`InProcessArtifactSink` / `BlobArtifactSink`)+ `artifact_sinks` Registry;`AgentResult.artifacts` 挂交付物引用 |
| `agent/builtin/deep_research.py` | `DeepResearchAgent`:planner 分解 + `Coordinator` 并行检索 + `LlmAgent` 综合的纯组合预置 agent |
| `sandbox/seam.py` | Sandbox 缝:`InProcessSandbox`(受限白名单表达式求值器,**先验规模守卫** / 工作量预算 / 按估算字节的内存预算 / 值上限 / 输出上限 / 协作式 timeout(第二道闸,打断不了单个内建调用),时钟可注入)+ `sandboxes` Registry。`subprocess` / `container` 槽是**占位、尚未实现**:`make("subprocess")` 必抛 `SeamError`;`make("container")` 缺 `[sandbox]` extra 时抛 `ImportError`,装了也必抛 `SeamError` |
| `skills/` | Skill 缝:`SkillSpec` + `FixtureSkill`(脚本经 Sandbox 执行)+ `SkillBundle`(目录加载)+ `skill_as_function_tool`(桥成 `FunctionTool`)+ `skill_registry` |
| `agent/tool_using.py` | `ToolUsingAgent`:**离线确定性**多步循环(`SyntaxToolPolicy` 按 `<tool>: <arg>` 语法路由),带 `max_steps` 守卫;实现 `Agent` 协议 |
| `agent/function_calling.py` | `FunctionCallingAgent`:**真 LLM function-calling** 多步循环——把 `FunctionTool` schema 喂给 `chat(tools=)`,模型回 tool_calls → 执行 → 以 OpenAI tool 角色喂回 → 再 chat,直到出文本或触顶;底层换任意 provider 不改一行。工具异常归一成只含错误码与类型名的 tool 消息喂回(`fail_fast=True` 恢复冒泡);usage 逐轮累加。实现 `Agent` 协议 |
| `tools/function_tool.py` | `FunctionTool`(带 JSON-schema、接 dict 参数的结构化工具)+ `@function_tool` 装饰器(从签名/注解/docstring 自动推 schema) |
| `agent/as_tool.py` | `AgentTool`:把一个 `Agent` 桥成 `Tool`,让督导 agent 通过工具调用把子任务派给专精子 agent(**分层 / 督导式多 agent**,可层层嵌套) |
| `tools/tool.py` | `Tool` 协议 + `EchoTool` / `CalcTool`(安全算术求值,表达式长度 / 嵌套深度 / 幂与乘法位数有上限);结果带 provenance。`tool_registry`:spec 选工具 + **entry-point 第三方工具自动发现**(group `corespine.tool`)。**运行时可把 ragspine RAG 插为一个 Tool**(见下) |
| `orchestration/coordinator.py` | `Coordinator`:把多个 agent **顺序 / 并行 / 流水线**(output→input 链式)跑、保序收集 `AgentResult`;**弹性容错**(`resilient=True`)把单 agent 异常归一为家族错误 dict 塞进 `AgentResult.error`、一个坏 agent 不炸整批;`run_parallel` 可选 `timeout` / `task_timeout`,挂死的 agent 以 `orchestration.timeout` 结果返回 |
| `orchestration/chain.py` | `ChainAgent`:把一串 agent 串成**单个 `Agent`**(流水线即一等可组合单元),可再进 `Coordinator` / 被 `AgentTool` 当工具 / 套 chain |
| `protocol/mcp/seam.py` | `McpClient` / `McpServer` 协议 + `OfflineMcpStub`(进程内回环)+ **`McpClientTool`(把 MCP 工具桥成 `Tool`)**。`mcp_clients["real"]` 是**占位、尚未实现**:缺 `[mcp]` extra 抛 `ImportError`,装了 extra 也必抛 `SeamError`——装 extra 并不能直接用 |
| `protocol/a2a/seam.py` | `A2AAgent` 协议 + `OfflineA2AStub`(进程内回环)+ **`A2AAgentAdapter`(把 A2A agent 桥成 `Agent`)**。`a2a_agents["real"]` 是**占位、尚未实现**:缺 `[a2a]` extra 抛 `ImportError`,装了 extra 也必抛 `SeamError` |
| `conformance.py` | 本包绑定的不变量:`AGENT_INVARIANTS`(步产出 / provenance / 隐私 trace)、`TOOL_INVARIANTS`(结果 provenance)、`POLICY_INVARIANTS`(决策形状 / 不幻觉工具 / 可终止 / 纯函数 / 数据不当指令)、`LLM_INVARIANTS` / `STREAMING_INVARIANTS`(OpenAI 形状 / 流式拼接等价)、`SANDBOX_INVARIANTS`(含超时生效、值放大型表达式有界)、`SKILL_INVARIANTS`、`MIDDLEWARE_INVARIANTS`、`ARTIFACT_INVARIANTS`、`APPROVAL_INVARIANTS`、`APPROVAL_ENFORCEMENT_INVARIANTS`(未批准执行 0 次 / 重跑不绕过 / 改参重审 / 门故障 fail-closed,参数化所有工具执行形态)、`TOOL_TRACE_INVARIANTS`(未知工具名不进 trace) |

## 运行时组合 ragspine(ADR 0001 D4b)

spineagent **不**在包层面依赖 ragspine。但可在**运行时**把 ragspine 的 RAG 检索包成一个实现了
`Tool`(或 MCP server)协议的适配器,插给某个 agent 调用——松耦合、可选,方向只能 spineagent→ragspine。
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
.venv/bin/python -c "import spineagent"
```

## 30 秒上手

```python
from corespine import MockProvider, InProcessPrivacyTraceSink
from spineagent import LlmAgent, FunctionAgent, Coordinator, EchoTool, OfflineMcpStub
from spineagent.protocol.mcp.seam import McpTool

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
from spineagent import tool_registry
print(tool_registry.make("calc").run("6/2").output)   # '3'
print("calc" in tool_registry.names())                # True

# 会用工具的多步 agent:离线确定性 policy 按 `<tool>: <arg>` 语法路由,$prev 把上一步输出喂回
from spineagent import ToolUsingAgent, SyntaxToolPolicy, CalcTool
solver = ToolUsingAgent("solver", SyntaxToolPolicy(), [CalcTool()])
print(solver.step("calc: 2 + 3\ncalc: $prev * 2").output)   # '10'(2+3=5,再 *2=10)

# 分层督导式多 agent:把子 agent 用 AgentTool 暴露成工具,督导 agent 通过工具调用派活给它
from spineagent import AgentTool
calculator = ToolUsingAgent("calculator", SyntaxToolPolicy(), [CalcTool()])
supervisor = ToolUsingAgent("supervisor", SyntaxToolPolicy(), [AgentTool(calculator)])
print(supervisor.step("calculator: calc: 2+3").output)      # '5'(督导派给子 agent,子 agent 再用工具)

# MCP 离线回环:注册 + 调用,零网络
stub = OfflineMcpStub()
stub.register_tool(McpTool("upper"), lambda a: {"result": a["s"].upper()})
print(stub.call_tool("upper", {"s": "hi"}))     # {'result': 'HI'}

# 跨缝组合:把上面那个 MCP 工具桥成 Tool,交给会用工具的 agent 在循环里驱动(零网络)
from spineagent import McpClientTool
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
pip install "spineagent[openai]"      # OpenAI 及一切「OpenAI 兼容」端点
pip install "spineagent[anthropic]"   # 或 [cohere] / [gemini] / [bedrock]
```

> **如实说明:`[mcp]` / `[a2a]` / `[sandbox]` 三个 extra 目前只装上第三方 SDK,对应的真实后端是
> 占位、尚未实现。** `mcp_clients.make("real")`、`a2a_agents.make("real")`、`sandboxes.make("container")`
> 在装了 extra 后仍必抛 `SeamError`;`sandboxes.make("subprocess")` 与 `tool_policies.make("llm")`
> 无论装什么都必抛 `SeamError`。离线默认(`OfflineMcpStub` / `OfflineA2AStub` / `InProcessSandbox` /
> `SyntaxToolPolicy`)是目前唯一可用的实现;真实客户端需使用者按官方 SDK 自行接入并注册进对应
> Registry。LLM 的 `[openai]` / `[anthropic]` / `[cohere]` / `[gemini]` / `[bedrock]` 是真实实现。

```python
from spineagent import OpenAICompatProvider, AnthropicProvider, GeminiProvider, LlmAgent

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
> (绝不把它们套进 OpenAI 形状 = 不 shim)。默认离线路径仍是 `MockProvider`,`import spineagent`
> 永远零网络 SDK(真实 SDK 仅在选用对应 extra 时延迟 import)。reasoning / citations 等扩展本期丢弃。
