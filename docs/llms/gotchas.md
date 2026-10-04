# spineagent — gotchas（陷阱与约定）

给 LLM / agent 的「别踩」清单。每条都对应真实代码行为。

## 1) provider 的输出是 OpenAI ChatCompletion 形状(不是各家 native）

所有 provider 适配器都实现 corespine 的 `LLMProvider` 协议:`chat(messages, *, tools=None)` 返回
`corespine.llm.provider.ChatCompletion`,**统一 OpenAI 形状**——无论后端是 Anthropic / Gemini /
Cohere / Bedrock 还是 OpenAI 兼容端点。取结果固定这么写:

```python
completion.choices[0].message.content        # 文本(可能为 None)
completion.choices[0].message.tool_calls     # tuple[ToolCall, ...] | None
completion.choices[0].message.tool_calls[0].function.name       # 工具名
completion.choices[0].message.tool_calls[0].function.arguments  # JSON 字符串(不是 dict!)
completion.choices[0].finish_reason          # 'stop' / 'tool_calls' / 'length' / 'content_filter'
completion.usage.prompt_tokens / .completion_tokens / .total_tokens   # usage 可能为 None
```

- `function.arguments` 是 **JSON 字符串**,要 `json.loads(...)` 才是 dict(`FunctionCallingAgent` 内部已这么做)。
- 别假设拿到的是某家 native 响应对象——native → OpenAI 的转换在各适配器内部完成,对外只有这一种形状。

## 2) 离线默认是 MockProvider,它不会 function-calling

不传真实 provider 时,默认走 `corespine.MockProvider`:对同一对话恒定回
`[mock:<hex>] <最后一条 user 文本>`。它**故意不伪造 `tool_calls`**(离线不假装会推理)。后果:

- `LlmAgent(name, MockProvider())` 只做确定性回声,适合跑通管道 / 断言,**不**会真做规划或工具选择。
- `FunctionCallingAgent(..., MockProvider(), tools=[...])` 因 mock 不回 tool_calls,会**第一步就出文本**,
  绝不进工具循环。要演示 / 测试 function-calling 循环,注入一个会回 `tool_calls` 的 provider(真实
  后端,或脚本化 fake——见 `recipes.md` §10)。
- 离线想要确定性的「工具循环」,用 `ToolUsingAgent` + `SyntaxToolPolicy`(按 `<tool>: <arg>` 语法路由),
  而不是 `FunctionCallingAgent`。

## 3) 真实后端要装对应 extra(否则友好报错,而非裸 ModuleNotFoundError）

`import spineagent` 绝不拉任何网络 SDK。真实后端在**构造适配器**(且未注入 `client=`)时才延迟 import:

| 后端 | provider | 安装 |
|---|---|---|
| OpenAI / 一切 OpenAI 兼容端点 | `OpenAICompatProvider` | `pip install "spineagent[openai]"` |
| Anthropic | `AnthropicProvider` | `pip install "spineagent[anthropic]"` |
| Cohere | `CohereProvider` | `pip install "spineagent[cohere]"` |
| Google Gemini | `GeminiProvider` | `pip install "spineagent[gemini]"` |
| AWS Bedrock | `BedrockConverseProvider` | `pip install "spineagent[bedrock]"` |
| MCP 真实 SDK | `mcp_clients.make("real")` / `load_mcp_sdk()` | `pip install "spineagent[mcp]"` |
| A2A 真实 SDK | `a2a_agents.make("real")` / `load_a2a_sdk()` | `pip install "spineagent[a2a]"` |
| 全部 | — | `pip install "spineagent[all]"` |

缺对应 extra 去构造真实适配器,会得到指明「pip install spineagent[<extra>]」的友好 `ImportError`。
注意:`[gemini]` 装的是 `google-genai`,`[a2a]` 装的是 `a2a-sdk`(import 名 `a2a`)。

## 4) SeamError 表示「缝槽存在但真实实现未接入」

家族统一约定:某些缝的 `real` / `llm` 槽是**占位**,本包只提供缝 + 离线 stub:

- `tool_policies.make("llm")` —— 直接抛 `corespine.errors.SeamError`(无 SDK 可 import,留待接真
  provider 解析 function-calling 后接入)。
- `mcp_clients.make("real")` / `a2a_agents.make("real")` —— **两阶段**:未装对应 extra 时先抛
  `ImportError`(缺 `[mcp]` / `[a2a]`);装了 extra 后才走到 `SeamError`(SDK 在但适配器留待使用者
  按官方 SDK 接入)。

经验法则:`ImportError` = 缺 extra(照提示 `pip install spineagent[<extra>]`);`SeamError` = 你选了
一个尚未接入的真实槽。两者都意味着:要么改用离线默认(`offline`),要么自己接入真实实现。

## 5) FunctionTool 与 Tool 是两种东西,别混用

- `Tool` 协议:`run(arg: str) -> ToolResult`。给 `ToolUsingAgent`(离线语法路由)用。例:`CalcTool`、
  `EchoTool`、`McpClientTool`、`AgentTool`。
- `FunctionTool`(及 `@function_tool`):`schema()` + `invoke(args: dict) -> str`。给 `FunctionCallingAgent`
  (真 LLM function-calling)用。它**不**实现 `Tool.run`。

把 `@function_tool` 装出来的对象丢进 `ToolUsingAgent`(或反过来把 `CalcTool` 丢进 `FunctionCallingAgent`)
会出错——它们走的是不同的工具协议。

## 6) `$prev` 只在 ToolUsingAgent 里、且首步替换为空串

`ToolUsingAgent` 在执行工具前把参数里的字面量 `$prev` 替换为**上一步观测的输出**。若**首步**就引用
`$prev`(尚无上一步),替换为空串——余下参数能否被工具处理由工具自身决定(如 `CalcTool` 对空串会抛
`ValueError`,异常照常上抛)。错误处理 / 重试不在本层(交编排层 / 调用方)。`$prev` 是 `ToolUsingAgent`
的语义,**不**适用于 `FunctionCallingAgent`(后者由模型自己串联多轮)。

## 7) max_steps 计的是工具调用次数,不是 LLM 轮数语义要分清

- `ToolUsingAgent(max_steps=N)`:N = **最多调用多少次工具**;收尾决策本身不占预算。触顶强制收尾、绝不死循环。
- `FunctionCallingAgent(max_steps=N)`:N = 最多 **chat 轮数**(每轮一次 `model.chat`);触顶兜底返回
  `"(reached max_steps without a final answer)"`。两者都保证产出非空。

## 8) 编排默认 fail-fast;弹性容错要显式开 resilient

`Coordinator.run_*(...)` 默认 `resilient=False`——任一 agent 抛异常即冒泡。要「坏 agent 不炸整批」,
传 `resilient=True`:异常被归一为 `error_to_dict(exc)` 塞进该步 `AgentResult.error`(`r.ok` 为 `False`),
顺序 / 并行跑完其余 agent;**流水线**则在失败处停止(下游拿不到输入)。`ChainAgent.step` 内部走流水线
但**不**透传 resilient,始终 fail-fast。

## 9) trace 只吃元数据,塞正文会被拒

任何 `step` / 编排方法可选的 `trace=` 只接受**元数据**(agent 名、步序、计数、长度、耗时)。
`corespine.InProcessPrivacyTraceSink` 「构造即保证」:写入命中 `FORBIDDEN_KEYS`(`content` / `text` /
`answer` / `value` / `prompt` / `completion` …)的字段会当场抛 `TraceError`。所以别期望从 trace 里
读回任务或输出正文——那是设计上挡死的。

## 10) provider 默认 max_tokens 与默认 model 是真实的、要留意

各 provider 有真实默认值(来自源码),用前确认是否符合预期:
- `AnthropicProvider` 默认 `model="claude-opus-4-8"`、`max_tokens=4096`。
- `OpenAICompatProvider` / `BedrockConverseProvider`:`model` **必填**(无通用默认);`max_tokens` 默认 4096(Bedrock 经 `extra` 传)。
- `CohereProvider` 默认 `model="command-r-plus"`;`GeminiProvider` 默认 `model="gemini-2.5-flash"`。
- 需要 thinking / 流式 / 其它后端特有参数,经各 provider 的 `extra=` 透传给底层 SDK 调用。reasoning /
  citations 等扩展本期不透出(统一规整为 OpenAI 形状时丢弃)。

## 11) 不要指望从 spineagent 拿 RAG

`spineagent` 不含任何 RAG 概念,也**不**在包层面依赖 `ragspine`。要做检索增强,在**运行时**把
ragspine(或任意检索能力)包成一个实现了 `Tool`(`run(arg)->ToolResult`)或 MCP server 的适配器,
插给某个 agent——方向只能 `spineagent → ragspine`,绝不写进 `dependencies`。

## 12) 审批闸在工具调用点上,不在 `ctx.tools` 上

`ApprovalMiddleware(gate, gated_tools=[...])` 不再看 `StepContext.tools`(那只是声明面);它把审批配置压进
当前上下文,真正的检查发生在 `FunctionCallingAgent` / `ToolUsingAgent` 每一次调用工具之前。几点要知道:
- 只有受审批工具**真的被调用**时才会抛 `ApprovalRejected` / `ApprovalPending`;不调用就不抛。
- request id 由「工具名 + 规范化参数」派生:同参重跑命中已落决议,**参数一变就要重新审批**。
- 批准不是一次性的(同工具同参数后续都放行);一次性的是 `ManualApprovalGate.resolve` 返回的 resume token。
- 恢复 = resolve 后原样重跑 step,会重放该步内已执行过的工具副作用。
- 自己起线程跑 agent 时,中间件作用域不会自动跟过去(`Coordinator.run_parallel` 已处理);需要跨任意线程
  的保证就用 `require_approval(tool, gate)` 把闸绑在工具上。自定义执行工具的 agent 要调 `enforce_tool_approval`。

## 13) 上游输出是数据,不是指令

`Coordinator.run_pipeline` / `ChainAgent` 把上游输出以 `TaskText` 数据段传给下游;附件、`$prev` 回灌的
工具结果、`AgentTool` / `McpClientTool` / `A2AAgentAdapter` 的返回同理。`SyntaxToolPolicy` 不解析数据段
里的 `<tool>: <arg>`。想让上游驱动下游执行工具,必须显式 `SyntaxToolPolicy(parse_untrusted=True)`。
对 `TaskText` 做 `+` / f-string / `strip` 会得到 plain str(= 指令),转手数据请用 `untrusted()` / `compose()`。

## 14) 工具抛异常不再炸掉 FunctionCallingAgent

工具函数抛的异常会被归一成 `error: tool failed [code=... type=...]` 的 tool 消息喂回模型(缺省不带异常消息
原文,`include_error_message=True` 才带);要旧的「直接冒泡」行为传 `fail_fast=True`。审批错误与
`KeyboardInterrupt` / `SystemExit` 不会被吞。`ToolUsingAgent` 仍让工具异常冒泡。

## 15) 沙箱的代价上界来自先验守卫,timeout 只是第二道闸;CalcTool 有上限

`InProcessSandbox.run(timeout=...)`(以及 `DEFAULT_LIMITS` 的 5 秒)在每个节点前后做协作式 deadline 检查,
超时判 `limit_exceeded`。**协作式超时无法中断单个内建调用**:`round(1, -10**7)` 这类在一个节点内部按参数的
**值**放大代价的调用,是靠先验规则(`round` 的 `|ndigits|` ≤ 2467、`int()` 数字串 ≤ 4300 字符、`sum` 只做
数值累加、幂 / 重复 / 拼接按结果规模预判)在调用前拒绝的;反复引用同一个大值的写法由工作量预算(`max_ops`,
`sorted(x)` 记 `len(x)` 单位)截住。要真正的抢占式超时,用 OS 级沙箱后端(子进程 / 容器)。`CalcTool` 拒绝超长(> 4096 字符)、过深(> 100 层)、超大幂 / 乘法结果的表达式,立即抛
`ValueError`。

## 16) failover 不会替坏请求「换一家再试」

适配器把 HTTP 400 / 413 / 422 归一为 `NonRetryableProviderError`,`FailoverProvider` 直接上抛、不冷却任何下游;
网络 / 超时 / 429 / 5xx / 鉴权类错误仍按可重试处理。自定义下游想表达「别重试」,抛 `NonRetryableProviderError`。

## 17) 重名工具直接报错

同一个 agent 里传两个同名工具,构造时抛 `ValueError`(以前是后一个静默覆盖前一个)。

