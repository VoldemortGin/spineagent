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
- request id 由「工具名 + 规范化参数 + 作用域」派生:**参数一变就要重新审批**,换一个作用域(会话 / 用户)也是。
- **批准缺省一次性**:一次批准只放行一次匹配调用,执行时核销;要放行多次用 `resolve(..., uses=N)`,
  `uses=None` 是旧的「有效期内不限次」模式(有重放风险,慎用)。
- **作用域必须显式**:会产生待审请求的门要求调用方提供作用域。门**声明**自己要不要(`requires_scope`:`AutoApprovalGate`
  为 `False`、`ManualApprovalGate` 为 `True`);**第三方门未声明时按「需要」处理**(fail-closed),缺作用域即报配置错误且
  **不调用**它的 `review`——若该门不会产生待审请求,请在门上声明 `requires_scope = False`。提供作用域的方式——
  `ApprovalMiddleware(scope=...)` / `require_approval(scope=...)` / 外层 `with approval_scope(...)`;都没有时在任何工具
  执行前抛 `ApprovalConfigError`(库不生成隐式作用域)。`AutoApprovalGate` 这类同步门不需要。作用域是不透明字符串,
  **必须在共享同一个门的所有调用方之间唯一**(建议 `f"{tenant_id}:{session_id}"`)——同作用域 + 同工具 + 同参数就是
  同一个请求、共用同一个批准。
- `ManualApprovalGate.resolve` 只接受已登记的待审请求(先 review 过)。请求表有界且 fail-closed:每个作用域最多 64 条
  「待审 + 未核销的已批准」、全表 16384 条(两个上限独立,可调;被拒绝 / 已核销的记录不占配额),满了**拒绝新请求**
  (`ApprovalGateError`),不会挤掉别人的。`FunctionCallingAgent` 一轮最多
  `max_tool_calls_per_turn=64` 个调用,超出整轮不执行。
- **审批人看完整参数**:`pending()` 里的 `request.arguments()` 是完整规范化参数(request id 哈希的就是它),审批 UI
  应展示它;`request.preview` 只作列表展示(长值截断一次并注明省略字符数与摘要)。缺省**不打码**;要在 preview 里
  藏某些字段,显式声明 `ApprovalMiddleware(sensitive_args={"login": ["password"]})`。两者都不进 trace / repr。
- **挂起后重跑是至少一次**:`raise` 模式下批准后重跑这个 run,此前各轮已执行过的工具(含 `send_email` 这类未受审批的)
  **会再执行一次**——库不记录、不复用任何跨 run 的工具结果。工具应幂等;或用 `on_approval="feed_back"`:挂起 /
  拒绝作为 tool 结果喂回模型,run 正常结束、没有重跑,调用方从 `result.held_approvals` 拿到 `request_id` / `scope`
  去审批,批准后由下一个任务执行。同一轮内不重放:`FunctionCallingAgent` 缺省**先审后行**——一轮 tool_calls 里有任何
  受审批调用未获批,整轮一个都不执行(`approve_before_execute=False` 恢复逐个执行)。
- 核销发生在**执行之前**:工具随后抛异常,这次批准也已用掉,要重试须重新批准。
- `gated_tools` 必须写确切工具名:通配符直接报错;能推断工具清单时,只差大小写 / 分隔符的名字报错,清单里找不到的
  名字只警告一次(全站共用名单、agent 变体缺这个工具都是合法配置;要严格就 `strict_names=True`)。名字校验只防笔误:
  按名字 gate 挡不住「同一函数以别名注册」——安全场景用 `require_approval(tool, gate)` 绑在工具对象上。
- **动态作用域不跨越调用方自建的线程**(`Coordinator.run_parallel` 已处理):工具函数里自己起线程 / 线程池跑子 agent
  时,`ApprovalMiddleware` 的闸在那条线程里不存在,名字校验也发现不了(外层恰好也注册了同名工具时尤其如此)。这种场景
  只有 `require_approval(tool, gate)`(绑在工具对象上)可靠;若必须用中间件,在起线程处用
  `pool.submit(bind_context(agent.step), task)` 把当前上下文带过去。自定义执行工具的 agent 要调 `enforce_tool_approval`。

## 12a) 多个受审批调用分布在多轮:`raise` 模式会指数重跑,必须用 `feed_back`

`on_approval="raise"`(缺省)遇到待审就抛 `ApprovalPending`,批准后**整个 run 重跑**;一次性批准被核销后,下一跑里
此前已执行的受审批调用会重新挂起。所以一个 run 里有 N 个受审批调用分布在 N 轮时,要批 2^N−1 次,第一个动作被执行
2^(N−1) 次(N=4:批 15 次、执行 8 次),且每次需要再批的请求与已执行过的那条 request id 相同——这是**至少一次**语义的
最坏形态,不是放行漏洞(每次执行都有一次批准),但缺省模式在这种流程里不可用。

- **何时必须 `on_approval="feed_back"`**:一个 run 里有 ≥2 个受审批调用且不在同一轮。`feed_back` 下挂起 / 拒绝作为
  tool 结果喂回模型、run 正常结束,没有重跑;每个动作恰好执行一次、N 个动作批 N 次(从 `result.held_approvals` 取请求,
  批准后由下一个任务执行)。
- 审批人看得出重复:重新登记的待审请求带 `ApprovalRequest.prior_executions`(此前已执行的次数,有界 / 短 TTL 的提示性
  计数),`pending()` 的 `preview` 首行注明「此前已执行过 n 次」,`ApprovalPending` 的消息与
  `context["prior_executions"]` 同样带出并提示改用 `feed_back`。
- `Coordinator.run_parallel`(非 resilient):一个分支挂起即整批冒泡、未开始的兄弟分支被取消,审批人一次只看到一条,
  批准后整批重跑、已获批的分支再执行一次。审批场景请让分支用 `on_approval="feed_back"`,或用 `resilient=True`
  (分支各自跑完,挂起的带 error,不取消兄弟);分支内的工具应幂等。

## 13) 上游输出是数据,不是指令

`Coordinator.run_pipeline` / `ChainAgent` 把上游输出以 `TaskText` 数据段传给下游;附件、`$prev` 回灌的
工具结果、`AgentTool` / `McpClientTool` / `A2AAgentAdapter` 的返回同理。`SyntaxToolPolicy` 不解析数据段
里的 `<tool>: <arg>`。想让上游驱动下游执行工具,必须显式 `SyntaxToolPolicy(parse_untrusted=True)`。
agent 的产出(`AgentResult.output`)在产出时就是数据:`down.step(up.step(t).output)` 不会执行上游文本里的指令。
对 `TaskText` 做 `+` / `%` / `format` / f-string / `join` / `strip` / `replace` / `split` / 切片 / `str()` / JSON 往返
都会得到 plain str(= 指令),转手数据请用 `untrusted()` / `compose()`。

## 14) 工具抛异常不再炸掉 FunctionCallingAgent

工具函数抛的异常会被归一成 `error: tool failed [code=... type=...]` 的 tool 消息喂回模型(缺省不带异常消息
原文,`include_error_message=True` 才带);要旧的「直接冒泡」行为传 `fail_fast=True`。审批错误与
`KeyboardInterrupt` / `SystemExit` 不会被吞。`ToolUsingAgent` 仍让工具异常冒泡。

## 15) 沙箱的代价上界来自先验守卫,timeout 只是第二道闸;CalcTool 有上限

`InProcessSandbox.run(timeout=...)`(以及 `DEFAULT_LIMITS` 的 5 秒)在每个节点前后做协作式 deadline 检查,
超时判 `limit_exceeded`。**协作式超时无法中断单个内建调用**:`round(1, -10**7)` 这类在一个节点内部按参数的
**值**放大代价的调用,是靠先验规则(`round` 的 `|ndigits|` ≤ 2467、`int()` 数字串 ≤ 4300 字符、`sum` 只做
数值累加、幂 / 重复 / 拼接按结果规模预判)在调用前拒绝的;反复引用同一个大值的写法由工作量预算(`max_ops`,
`sorted(x)` 记 `len(x)` 单位,文本按存储字节折算)截住;内存由**按估算字节计的内存预算**(`max_memory_bytes`,缺省
32 MiB,`None` 时 128 MiB 硬上限)兜住:每个值产生时就计入,列表字面量 / 调用实参在超限处立即停下,不会先把 1400 个
256 KB 的宽字符串全部造出来。预算是**累计**口径(临时值释放不回退),所以大量中间值的表达式可能比实际占用更早被拒。要真正的抢占式超时,用 OS 级沙箱后端(子进程 / 容器)。`CalcTool` 拒绝超长(> 4096 字符)、过深(> 100 层)、超大幂 / 乘法结果的表达式,立即抛
`ValueError`(语法错误、`10.0**400` 这类溢出也一样);深度按真正的嵌套计,长的连加 / 连乘不再被当成「过深」。

## 16) failover:「同一家重试」与「换一家」是两件事

`retryable=False`(`NonRetryableProviderError`,非瞬时 4xx)只表示别对**同一家**原样重试;余额不足 / 配额 /
鉴权 / 模型不存在 / 上下文超长这类常以 400 返回的错误,换一家完全可能成功,所以 `FailoverProvider` 缺省会回退:
与 provider 相关且持续失效的(401 / 402 / 403 / 404、余额 / 配额 / 计费 / 模型不存在)只冷却出错的那一家;上下文
超长和无法判定的 4xx 不冷却、**最多再试 1 家**——那家也以同类 4xx 拒绝就判定请求本身有问题,抛
`BadRequestProviderError`(一条畸形请求最多 2 次计费调用,而不是把整个池子打一遍)。OpenAI 风格错误体明确指出参数
校验失败(`invalid_request_error` + `param`)时适配器直接抛 `BadRequestProviderError`,不回退;想换取舍就注入
`failover_policy`。

## 17) 重名工具直接报错

同一个 agent 里传两个同名工具,构造时抛 `ValueError`(以前是后一个静默覆盖前一个)。

