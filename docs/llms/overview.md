# spineagent — overview

## 是什么

`spineagent` 是 Spine 家族的**通用多 agent 协作框架**:把「agent / tool / 编排 + MCP / A2A
协议缝 + 统一 LLM provider 适配层」收成一组**小而稳定的原语**。它依赖薄核 `corespine`,复用其
缝元模式与 observability / config 形状,但**不**含任何 RAG 概念。

它解决的问题:用一套**离线可跑、可复现、可断言**的原语,把「单个 agent 跑一步」「会用工具的
多步循环」「真 LLM function-calling」「顺序 / 并行 / 流水线编排多个 agent」「跨进程 / 跨框架的
MCP / A2A 桥接」「把任意真实 LLM 后端规范化成 OpenAI 形状」这些常见 agent 工程任务,统一到
**同一组协议 + 同一套缝注册表**之下——默认路径零网络、零重依赖,装上即可端到端跑;接真实后端时
只在对应缝里换实现,其余代码一行不改。

## 离线优先(最重要的设计取舍)

`import spineagent` **绝不**拉入任何网络 SDK。所有真实后端(LLM SDK、MCP / A2A SDK)都在
**用到时**才经可选 extra 延迟 import。默认 LLM 是 `corespine.MockProvider`:对同一对话恒定产出
同一个 `[mock:<hex>] <最后一条 user 文本>`(纯函数、零 key)。

代价是诚实的:`MockProvider` **不**伪造 `tool_calls`(离线不假装会 function-calling)。因此
离线的工具循环用 `ToolUsingAgent` + `SyntaxToolPolicy`(按任务文本里的 `<tool>: <arg>` 显式语法
确定性路由),而**真** LLM function-calling 用 `FunctionCallingAgent`,把任意真实 provider 喂进去。

## 核心概念

### Agent

最小执行单元:有 `name`,`step(task: str) -> AgentResult`(跑一步)。`AgentResult` 带
`agent`(provenance,产出它的 agent 名)、`output`、可选 `usage` / `error`。四个具体实现:

- `LlmAgent(name, provider)` — 用一个 corespine `LLMProvider` 跑单步(离线 `MockProvider`)。
- `FunctionAgent(name, fn)` — 把纯函数 `(str) -> str` 包成 Agent(测试 / 编排的轻量节点,无需 LLM)。
- `ToolUsingAgent(name, policy, tools)` — 在**一次 step()** 内跑「决策→调工具→喂回观测→再决策」的
  离线确定性多步循环,带 `max_steps` 守卫。
- `FunctionCallingAgent(name, model, tools)` — **真 LLM** 原生 function-calling 多步循环:把
  `FunctionTool.schema()` 喂给 `model.chat(tools=...)`,模型回 `tool_calls`→执行→以 OpenAI tool
  角色喂回→再 chat,直到出文本或触顶。

### Tool

agent 在一步里可调用的能力:有 `name`,`run(arg: str) -> ToolResult`(结果带 `tool` provenance)。
- `EchoTool` / `CalcTool`(安全算术求值,只认 `+ - * / % **`,绝不 eval 任意代码)。
- `FunctionTool` + `@function_tool` 装饰器:带 JSON-schema、接 dict 参数的**结构化**工具,给真
  function-calling 用(`schema()` 产出 OpenAI function-tool 形状,`invoke(args_dict)` 调底层函数)。
- `tool_registry`:`Registry[Tool]`,`make(spec)` 按名选工具(已注册 `echo` / `calc`),并支持
  entry-point group `corespine.tool` 第三方工具自动发现。

### 编排:Coordinator + ChainAgent

- `Coordinator(agents)`:把一组 agent **顺序 / 并行 / 流水线**跑、**保序**收集 `list[AgentResult]`。
  - `run_sequential` / `run_parallel`(线程池并发,结果仍按输入顺序返回)/ `run_pipeline`(链式:
    上一个 agent 的输出当下一个的输入)。
  - **弹性容错** `resilient=True`:单 agent 异常被归一为家族错误 dict 塞进 `AgentResult.error`,
    一个坏 agent 不炸整批(流水线则在失败处停止,因为下游拿不到输入)。
- `ChainAgent(name, agents)`:把一串 agent 串成**单个 Agent**(流水线即一等可组合单元),可再进
  `Coordinator` / 被 `AgentTool` 当工具 / 套进另一个 chain。

### 跨缝桥接三角

把不同原语互相适配,组合出分层 / 跨进程多 agent:

- `AgentTool(agent)` — Agent → Tool(督导式多 agent:督导 agent 通过工具调用把子任务派给子 agent,可嵌套)。
- `McpClientTool(name, client)` — MCP 工具 → Tool(让会用工具的 agent 透过 MCP 驱动外部 / 进程内工具)。
- `A2AAgentAdapter(remote)` — A2A agent → Agent(让远端 / 进程内 A2A agent 像本地 agent 一样进编排)。

### 协议缝:MCP / A2A

每条缝长一个样(家族统一元模式):**Protocol + 离线确定性默认 + Registry 工厂 + 真实 SDK 经可选 extra 延迟 import**。

- MCP:`McpClient` / `McpServer` 协议 + `OfflineMcpStub`(进程内回环,同时满足 client 与 server)
  + `mcp_clients` Registry(`offline` / `real`)。真实 SDK 走 `[mcp]` extra。
- A2A:`A2AAgent` 协议 + `OfflineA2AStub`(进程内回环)+ `a2a_agents` Registry(`offline` / `real`)。
  真实 `a2a-sdk`(import 名 `a2a`)走 `[a2a]` extra。

两条缝的 `real` 槽是**占位、尚未实现**:缺 extra 抛 `ImportError`,装了 extra 也必抛 `SeamError`
(「缝槽存在但真实实现未接入」),由使用者按官方 SDK 自行接入——本包只提供缝 + 离线 stub。

### middleware / approval / sandbox / skills / artifact / deep research

- **middleware**:`MiddlewareAgent(name, agent, middlewares)` 洋葱式包住任意 Agent(before 正序、after
  逆序),内置 `TokenUsage` / `Summary` / `DynamicTool` / `Attachment`;`StepContext.cleanups` 无论成败都执行。
- **approval(审批门 / Wait)**:`ApprovalGate`(`AutoApprovalGate` 策略表 / `ManualApprovalGate` 人工挂起 +
  一次性 resume token)。审批是**工具调用点上的强制闸**(ADR 0002):`ApprovalMiddleware` 把审批配置压进
  当前上下文,`FunctionCallingAgent` / `ToolUsingAgent` 在**每一次**真实调用工具前按「工具名 + 规范化参数」
  review;未批准不执行,门故障 fail-closed;`require_approval(tool, gate)` 把闸直接绑进工具本身。
- **sandbox**:`InProcessSandbox` 受限白名单表达式求值器(先验规模守卫、工作量预算、值上限、输出上限;
  协作式 timeout 只是第二道闸,打断不了单个内建调用);
  `subprocess` / `container` 是占位、尚未实现。
- **skills**:manifest + 受限脚本的能力包,脚本经 Sandbox 执行,可桥成 `FunctionTool`。
- **artifact**:`ArtifactSink` 存交付物,`AgentResult.artifacts` 挂引用。
- **DeepResearchAgent**:分解 → 并行检索 → 综合的纯组合预置 agent。

### 信任边界:指令 vs 数据(ADR 0003)

只有调用方直接给 `agent.step()` 的 task 文本是**指令**。pipeline 上游输出、附件、工具结果、MCP / A2A
对端返回都是**数据**:在源头被标为 `TaskText` 不可信段,`SyntaxToolPolicy` 绝不把其中的 `<tool>: <arg>`
当工具调用执行(需要旧行为显式 `SyntaxToolPolicy(parse_untrusted=True)`)。真 LLM 读到数据里的「指令」
属于提示注入,由审批闸与工具最小授权兜底。

### LLM 适配器(对外统一 OpenAI ChatCompletion 形状)

所有 provider 都实现 corespine 的 `LLMProvider` 协议:`chat(messages, *, tools=None) -> ChatCompletion`,
**输入是 OpenAI 风格的 messages / function-tools,输出是 OpenAI 形状的 `ChatCompletion`**
(`choices[0].message.{content, tool_calls}`、`finish_reason`、`usage.{prompt,completion,total}_tokens`)。

- 离线默认:`corespine.MockProvider`(确定性,不伪造 tool_calls)。
- OpenAI 兼容生态(OpenAI / Azure / Together / Groq / DeepSeek / Mistral / xAI / Qwen / Moonshot /
  Ollama / vLLM / OpenRouter / LiteLLM…):`OpenAICompatProvider(model, base_url=...)` 一把梭(`[openai]` extra)。
- 非 OpenAI 原生后端,各有原生适配器(在内部把 native 响应转回 OpenAI `ChatCompletion`,不 shim):
  `AnthropicProvider`(`[anthropic]`,默认 `claude-opus-4-8`)、`CohereProvider`(`[cohere]`)、
  `GeminiProvider`(`[gemini]`)、`BedrockConverseProvider`(`[bedrock]`,Converse API 跨模型同形)。
- `llm_providers` Registry:`mock` / `openai` / `anthropic` / `cohere` / `gemini` / `bedrock`。

正因输出统一,`LlmAgent` / `FunctionCallingAgent` / 编排全程只认 `LLMProvider` 协议——把
`MockProvider` 换成任意真实适配器,agent / 工具循环 / 编排代码一行不改。

## 隐私安全 trace(机制借 corespine)

所有 `step` / 编排方法可选接收一个 `corespine` 的 `TraceSink`。实现只往里记**元数据**(agent 名、
步序、计数、长度、耗时),**绝不**记任务 / 参数 / 输出正文。`corespine.InProcessPrivacyTraceSink`
「构造即保证」:若试图写入受限字段(`content` / `text` / `answer` / `value`…,见 `FORBIDDEN_KEYS`),
`emit` 当场抛 `TraceError`。本包再用 conformance「按值」补一道防线。trace 里的工具名 / agent 名只取
本地注册 / 登记的名字:模型编造的未知工具名记 `<unknown>`,A2A 对端自报名不进 trace。

## conformance(机制借 corespine,保证由本包绑定)

`corespine.ConformanceSuite` 提供「实现 × 不变量」笛卡尔积的**机制**;具体不变量由本包绑定并导出:
- `AGENT_INVARIANTS` — 步必产出非空、结果可溯源到 agent、步级 trace 隐私安全。
- `TOOL_INVARIANTS` — 工具结果可溯源、调用必产出非空。
- `POLICY_INVARIANTS` — 决策是 ToolCall/Finish 之一、不幻觉不存在的工具、空工具集必收尾且非空、`decide` 是纯函数、数据段绝不变成工具调用。
- `LLM_INVARIANTS` / `STREAMING_INVARIANTS` / `SANDBOX_INVARIANTS`(含超时生效)/ `SKILL_INVARIANTS` /
  `MIDDLEWARE_INVARIANTS` / `ARTIFACT_INVARIANTS` / `APPROVAL_INVARIANTS`。
- `APPROVAL_ENFORCEMENT_INVARIANTS` — 参数化所有会执行工具的 agent 形态:未批准执行 0 次、重跑不绕过、
  改参数须重新审批、门故障 fail-closed、未受审批工具不受影响。
- `TOOL_TRACE_INVARIANTS` — 模型编造的未知工具名绝不进 trace。

任何号称 Agent / Tool / ToolPolicy 的(含第三方)实现,都可丢进对应不变量包跑 conformance——没过直接红,而非埋雷。

## 与 corespine 的关系

`spineagent` 的唯一运行时依赖是 `corespine`(薄核)。spineagent **消费** corespine 的缝,从不被它
反向依赖,也不含 corespine 已有的底层原语(LLM 缝形状、trace、registry、errors 都来自 corespine)。
方向:`spineagent → corespine`,单向。spineagent 也**不**在包层面依赖 `ragspine`;可在**运行时**
把 ragspine 的 RAG 包成一个实现了 `Tool`(或 MCP server)协议的适配器插给某个 agent——松耦合、可选,
方向只能 `spineagent → ragspine`,绝不写进 `dependencies`。
