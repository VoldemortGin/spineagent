# spineagent — public API

完整公开 API。所有签名 100% 来自真实源码(`inspect.signature` 核对)。一切都从顶层
`import spineagent` 可达;`spineagent.__all__` 即下列名字的全集。导入名 = `spineagent`。

> **共享类型(来自 corespine)**:provider 的 `chat` 返回 `corespine.llm.provider.ChatCompletion`
> (字段:`choices: tuple[Choice, ...]`、`usage: Usage | None`、`model: str`、`id: str`、
> `created: int`、`object: str`)。`Choice(index, message, finish_reason="stop")`;
> `ResponseMessage(role="assistant", content: str|None=None, tool_calls: tuple[ToolCall,...]|None=None)`;
> `ToolCall(id, function, type="function")`;`FunctionCall(name, arguments="{}")`;
> `Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)`。
> `MockProvider`、`InProcessPrivacyTraceSink`、`TraceSink` 也来自 corespine。

---

## agent

### `class AgentResult`
`AgentResult(agent: str, output: str, usage: dict[str, int] | None = None, error: dict[str, object] | None = None, artifacts: tuple[ArtifactRef, ...] = (), held_approvals: tuple[dict[str, str], ...] = ())`
- `held_approvals`:`FunctionCallingAgent(on_approval="feed_back")` 本步没执行、而是喂回模型的审批挂起 / 拒绝,每项
  `{"code", "request_id", "scope", "tool"}`,给调用方(不是模型)完成审批;`MiddlewareAgent` 原样保留,其它组合 agent
  不汇总(审批人侧以 `gate.pending()` 为准)。
- frozen dataclass。一次 agent 步的结果。`agent` = provenance(产出它的 agent 名)。
- 属性 `ok -> bool`:`self.error is None`。
- 契约:成功路径 `error is None`;`error` 仅由编排层弹性模式(`Coordinator(..., resilient=True)`)
  捕获 `step` 异常时填充,值是 `corespine.errors.error_to_dict(exc)` 的归一 dict(含 `code` /
  `retryable` / `message` / `context`)。`run_parallel` 超时的任务也以 `error.code == "orchestration.timeout"` 返回。
- `artifacts`:本步产出的交付物引用(见 artifact 缝);`ChainAgent` / `AgentTool` + `ToolUsingAgent` 会透传汇总。
- `merge_usage(*usages) -> dict[str, int] | None`(`spineagent.agent.agent`):逐键累加 usage,全 None 返回 None。

### `class Agent` (Protocol, runtime_checkable)
- 属性 `name: str`;方法 `step(self, task: str, *, trace: TraceSink | None = None) -> AgentResult`。
- 所有 agent 实现都满足它,故可互换进编排 / 桥接。

### `class LlmAgent`
`LlmAgent(name: str, provider: LLMProvider, *, system: str = "")`
- `step(task, *, trace=None) -> AgentResult`:把 `task`(+ 可选 `system`)按 OpenAI messages 喂给
  `provider.chat`,取 `choices[0].message.content` 作 `output`,带 `usage`。
- 离线传 `corespine.MockProvider()`;线上传任意真实 provider 适配器,代码不变。

### `class FunctionAgent`
`FunctionAgent(name: str, fn: Callable[[str], str])`
- `step(task, *, trace=None) -> AgentResult`:`output = fn(task)`。无需 LLM,做编排 / 测试节点。

### `class ToolUsingAgent`
`ToolUsingAgent(name: str, policy: ToolPolicy, tools: Iterable[Tool], *, max_steps: int = 8)`
- `step(task, *, trace=None) -> AgentResult`:在**一次** step() 内循环——`policy.decide(...)` 决定
  `ToolCall`(按名取 Tool 执行、观测追加进 history)或 `Finish`(返回最终答案)。
- `$prev`:工具参数里的字面量 `$prev` 在执行前替换为上一步观测输出(首步无上一步则替换为空串);
  拼进来的观测是**数据**(`TaskText` 不可信段),经 `AgentTool` 传给子 agent 时不会被当指令解析。
- 审批:每次真实调用工具前调 `enforce_tool_approval(工具名, {"arg": 实参})`(见 approval 缝)。
- 重名工具构造即抛 `ValueError`;工具结果的 `usage` / `artifacts` 汇总进返回的 `AgentResult`。
- `max_steps` = **最多调用多少次工具**(收尾不占预算);触顶强制收尾,绝不死循环。
- 实现 `Agent` 协议,可直接进 `Coordinator` / `ChainAgent` / 被 `AgentTool` 包成工具。

### `class FunctionCallingAgent`
`FunctionCallingAgent(name: str, model: LLMProvider, tools: Iterable[FunctionTool], *, system: str = "", max_steps: int = 8, fail_fast: bool = False, include_error_message: bool = False, approve_before_execute: bool = True, on_approval: Literal["raise", "feed_back"] = "raise", max_tool_calls_per_turn: int = 64)`
- `step(task, *, trace=None) -> AgentResult`:**真 LLM** 原生 function-calling 多步循环——把每个
  `FunctionTool.schema()` 喂给 `model.chat(messages, tools=...)`;模型回 `tool_calls` 则逐个
  `parse_arguments` 校验 → `enforce_tool_approval` 审批 → `invoke`、以 OpenAI `tool` 角色消息喂回、
  再 chat;无 tool_calls 则出文本收尾。触顶 `max_steps` 兜底非空。`usage` 为各轮累加。
- 审批(ADR 0002 决策 5 / 5a):`approve_before_execute=True`(缺省)时**先审后行**——一轮 tool_calls 执行任何一个
  之前先预检整轮,有任何受审批调用未获批则整轮一个都不执行;`on_approval="feed_back"` 时本执行点的挂起 / 拒绝
  作为 tool 结果(`error: approval required [code=... request_id=...]`,不含作用域)喂回模型而不抛,同时记进
  `AgentResult.held_approvals`(每项 `{"code", "request_id", "scope", "tool"}`,给调用方完成审批)。`raise` 模式下
  挂起后重跑整个 run 是**至少一次**语义:此前各轮已执行过的工具会再次执行(库不复用任何跨 run 的结果);多个受审批调用分布在
  N 轮时按 2^N 增长,此类流程**必须**用 `feed_back`(`ApprovalPending` 在同一请求第二次挂起时带 `context["prior_executions"]` 并提示)。
  一轮 tool_calls 超过 `max_tool_calls_per_turn` 时整轮不执行、不送审,每个调用喂回 `code=tool.too_many_calls`。
- 工具失败:工具函数抛的 `Exception`(审批错误除外)归一成 tool 消息
  `error: tool failed [code=<code> type=<异常类型名>]` 喂回模型(`code` 为 CorespineError 的 code,否则
  `tool.execution_failed`);`include_error_message=True` 才附异常消息原文(截断到 300 字符);`fail_fast=True` 恢复冒泡。
- `usage` 含工具函数里嵌套 agent 花掉的 token(经 `spineagent.agent.agent.collect_usage` / `report_usage`:直接调模型的
  `LlmAgent` / `FunctionCallingAgent` 与 `DeepResearchAgent` 把各自总数上报给最近的外层收集器,每份只计一次)。
  `KeyboardInterrupt` / `SystemExit` 与 `ApprovalError` 不吞。
- 未知工具名:回 `error: unknown tool ...` 给模型;trace 的 `tool` 字段记固定占位 `"<unknown>"`。
- 重名工具构造即抛 `ValueError`。
- 离线 `MockProvider` 不回 tool_calls → 直接出文本(诚实:离线不假装会 function-calling)。要真正
  跑工具循环,需注入会回 `tool_calls` 的 provider(真实后端,或测试用脚本化 fake)。

### `class AgentTool`
`AgentTool(agent: Agent, *, name: str | None = None)`
- 实现 `Tool` 协议。`name` 默认取 `agent.name`。
- `run(arg: str) -> ToolResult`:对子 agent 跑一步,把 `output` 包成带 provenance 的 `ToolResult`。
- 用于分层 / 督导式多 agent(可层层嵌套)。子 agent 的 `usage` / `artifacts` 随 `ToolResult` 透传;
  `output` 标为数据(`TaskText`)。子 agent 抛异常照常上抛(错误处理归编排层 / 调用方)。

---

## tool-policy 缝(会用工具的 agent 的「大脑」)

### `class ToolPolicy` (Protocol, runtime_checkable)
- `decide(self, task: str, *, tools: tuple[str, ...], history: tuple[Observation, ...]) -> Action`
- 给任务 + 可用工具名集 + 历史观测,定下一个动作。约定是**无状态纯函数**(同输入恒同输出)。

### `class ToolCall`
`ToolCall(tool: str, arg: str)` — frozen dataclass。决定:调一个工具(`arg` 中字面量 `$prev` 由 agent 侧替换)。

### `class Finish`
`Finish(answer: str)` — frozen dataclass。决定:收尾给最终答案(约定非空)。

### `Action`
`Action = ToolCall | Finish`(`typing.TypeAlias`,PEP 604 联合;`isinstance` 分发)。

### `class Observation`
`Observation(tool: str, arg: str, output: str)` — frozen dataclass。一步执行的观测,喂回循环。

### `class SyntaxToolPolicy`
`SyntaxToolPolicy(*, parse_untrusted: bool = False)` — 离线确定性默认实现。`decide(...)` 按任务文本里
`<tool>: <arg>` 显式语法 + 工具名集合确定性路由:游标 = `len(history)`,第 cursor 条工具指令尚存则
`ToolCall(该行工具名, 该行参数)`,指令耗尽则 `Finish`(把非指令正文行 + 最后一步观测拼成非空答案)。
**不**假装 LLM 推理。**只解析可信行**:含任何不可信字符(`TaskText` 数据段)的行按正文处理;
`parse_untrusted=True` 恢复旧的「整段都解析」行为(仅当上游确实可信)。

### `tool_policies`
`Registry[ToolPolicy]`(seam 名 `tool_policy`)。
- `tool_policies.make(spec, **kwargs) -> ToolPolicy`、`tool_policies.names() -> list[str]`。
- 已注册:`"offline"`(→ `SyntaxToolPolicy`)、`"llm"`(真实推理式占位,**调用即抛 `SeamError`**——
  留待接真 provider 解析 function-calling 后接入)。

---

## tools

### `class ToolResult`
`ToolResult(tool: str, output: str, usage: dict[str, int] | None = None, artifacts: tuple[ArtifactRef, ...] = ())` — frozen dataclass。`tool` = provenance(产出它的工具名);`usage` / `artifacts` 供 `AgentTool` 透传。

### `class Tool` (Protocol, runtime_checkable)
- 属性 `name: str`;方法 `run(self, arg: str) -> ToolResult`。

### `class EchoTool`
`EchoTool()`,`name = "echo"`。`run(arg) -> ToolResult`:原样回显 `arg`。

### `class CalcTool`
`CalcTool()`,`name = "calc"`。`run(arg) -> ToolResult`:安全求值算术表达式(白名单 `+ - * / % **`
与一元 `+ -`,整数结果去掉 `.0`);非算术节点抛 `ValueError`,绝不 eval 任意代码。有界:表达式 ≤ 4096
字符、**真正的**嵌套深度 ≤ 100(同一层的左结合连加 / 连乘不计深度,`1+1+…+1` 不受项数限制)、幂指数绝对值
≤ 10000、幂 / 乘法结果 ≤ 8192 位。越界、语法错误、数值溢出(如 `10.0**400`)一律抛 `ValueError`;除零照常
抛 `ZeroDivisionError`。

### `index_tools_by_name`(`spineagent.tools.tool`)
`index_tools_by_name(tools: Iterable[T]) -> dict[str, T]` — 按名建索引(保插入序),重名抛 `ValueError`。

### `tool_registry`
`Registry[Tool]`(seam 名 `tool`)。
- `tool_registry.make(spec, **kwargs) -> Tool`、`tool_registry.names() -> list[str]`。
- 已注册:`"echo"`、`"calc"`。支持 entry-point group `corespine.tool` 第三方工具自动发现。
- 注:变量名是 `tool_registry`(不是 `tools`),以避开与 `spineagent.tools` 子包同名。

### `class FunctionTool`
`FunctionTool(name: str, description: str, parameters: dict[str, Any], func: Callable[..., Any])` — dataclass。
- `schema() -> dict[str, Any]`:产出 OpenAI function-tool 形状 `{"type":"function","function":{name,description,parameters}}`,直接喂给 `LLMProvider.chat(tools=...)`。
- `invoke(arguments: dict[str, Any]) -> str`:用模型给的结构化 dict 调底层函数,`str(...)` 结果(回填进对话)。
- 注:`FunctionTool` 实现的是 `invoke`(dict 参数),**不**实现 `Tool.run`(str 参数);它专给
  `FunctionCallingAgent` 用,不能直接丢进 `ToolUsingAgent`。

### `function_tool`
`function_tool(func: Callable[..., Any] | None = None, *, name: str | None = None, description: str | None = None) -> Any`
- 装饰器:把普通函数包成 `FunctionTool`。`name` 默认 `func.__name__`,`description` 默认其 docstring,
  `parameters` 从签名 + 类型注解自动推 JSON-schema(无默认值的参数为 `required`;`str/int/float/bool/list/dict`
  映射到 JSON 类型,未识别落 `string`)。
- 用法:`@function_tool` 直接装,或 `@function_tool(name=..., description=...)` 覆盖。

---

## orchestration

### `class Coordinator`
`Coordinator(agents: Iterable[Agent], *, trace: TraceSink | None = None)`
- 属性 `agents -> list[Agent]`(副本)。
- `run_sequential(task: str, *, resilient: bool = False) -> list[AgentResult]`:逐个跑同一任务,保序。
- `run_parallel(task: str, *, max_workers: int | None = None, resilient: bool = False, timeout: float | None = None, task_timeout: float | None = None, clock: Callable[[], float] = time.monotonic) -> list[AgentResult]`:
  线程池并发跑同一任务,结果仍按 agent 输入顺序返回(`max_workers` 默认 = agent 数)。每个分支在调用方
  `contextvars` 上下文的副本里跑(审批作用域随之生效;带 / 不带超时两条路径都是)。`timeout`(整批,自调用起)/ `task_timeout`
  (单任务,自该任务开始跑起)缺省不限;到点未返回的任务以 `AgentTimeoutError`(code
  `orchestration.timeout`,**不可重试** `retryable=False`)归一的 `AgentResult.error` 返回,不挂住整批。
  **超时不终止线程**:挂死线程无法强杀,仍在后台跑、可能继续产生副作用——别据此直接重试同一任务。
- `bind_context(fn) -> Callable`(模块级,顶层亦导出):在调用时刻复制当前 `contextvars` 上下文并绑到 `fn` 上,
  交给自建线程 / 线程池执行时 `fn` 在这份副本里跑(`pool.submit(bind_context(agent.step), task)`)。线程池缺省
  **不**传播上下文:不包这一层,`ApprovalMiddleware` 的动态作用域到不了自建线程里的执行点。安全场景仍首选
  `require_approval`(静态绑定在工具对象上,不依赖上下文)。
- `run_pipeline(task: str, *, resilient: bool = False) -> list[AgentResult]`:链式——上一个 agent 的
  `output` 以**数据**身份(`untrusted(...)`)作下一个的输入,保序收集每段;下游指令解析器不执行其中的
  指令语法(ADR 0003)。
- 弹性容错 `resilient=True`:单 agent 异常归一为 `error_to_dict(exc)` 塞进该步 `AgentResult.error`,
  批次继续(顺序 / 并行跑完其余;流水线在失败处停止)。`resilient=False`(默认)= fail-fast,异常冒泡。
- 编排级 trace 只记 `mode` / `agent_count` / `failures` / `took_ms`,绝不记正文。

### `class ChainAgent`
`ChainAgent(name: str, agents: Iterable[Agent])`
- 实现 `Agent` 协议。`step(task, *, trace=None) -> AgentResult`:复用 `Coordinator(...).run_pipeline(task)`
  把任务逐段传递,返回末端 agent 输出(provenance = chain 名;空链退化为恒等透传)。失败 fail-fast 冒泡。
- 让流水线成为一等可组合单元:可进 `Coordinator` / 当 `AgentTool` 工具 / 套进另一个 chain。
- 返回的 `usage` 为各段累加,`artifacts` 为各段按序拼接。

---

## protocol: mcp

### `class McpTool`
`McpTool(name: str, description: str = "")` — frozen dataclass。一个 MCP 工具的最小描述。

### `class McpClient` (Protocol, runtime_checkable)
- `list_tools() -> list[McpTool]`、`call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]`。

### `class McpServer` (Protocol, runtime_checkable)
- `register_tool(tool: McpTool, handler: ToolHandler) -> None`、`tools() -> list[McpTool]`。
- `ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]`(模块内类型别名,未导出顶层)。

### `class OfflineMcpStub`
`OfflineMcpStub()` — 离线进程内回环,**同时**满足 `McpClient` 与 `McpServer`。
- `register_tool(tool, handler)`、`tools()`、`list_tools()`、`call_tool(name, arguments)`(未注册抛 `KeyError`)。

### `class McpClientTool`
`McpClientTool(name: str, client: McpClient, *, arg_key: str = "input", result_key: str = "result")`
- 实现 `Tool` 协议。`run(arg: str) -> ToolResult`:把单串 `arg` 包成 `{arg_key: arg}` 调
  `client.call_tool(name, ...)`,取结果 dict 的 `result_key` 转字符串,带 provenance(`tool = name`)。
- 最薄阻抗匹配:只做 str 单参 + 单键结果映射。结果缺 `result_key`(或不是 dict)抛
  `McpProtocolError`(code `mcp.invalid_result`);`output` 标为数据(`TaskText`)。

### `mcp_clients`
`Registry[McpClient]`(seam 名 `mcp_client`)。`make(spec, **kw)` / `names()`。
- 已注册:`"offline"`(→ `OfflineMcpStub`)、`"real"`(**占位、尚未实现**:缺 `[mcp]` extra 抛 `ImportError`,装了 extra 也必抛 `SeamError`)。

### `load_mcp_sdk() -> Any`
延迟 import 真实 MCP SDK(import 名 `mcp`);未装 `[mcp]` extra 时给「pip install spineagent[mcp]」友好报错。

---

## protocol: a2a

### `class A2ATask`
`A2ATask(task_id: str, text: str)` — frozen dataclass。一条跨 agent 任务(协议载荷本身)。

### `class A2AResult`
`A2AResult(task_id: str, output: str, agent: str)` — frozen dataclass。`agent` = provenance。

### `class A2AAgent` (Protocol, runtime_checkable)
- 属性 `name: str`;`card() -> dict[str, Any]`(能力描述);`send(task: A2ATask) -> A2AResult`。

### `class OfflineA2AStub`
`OfflineA2AStub(*, name: str = "offline-a2a", responder: Callable[[str], str] | None = None)`
- 离线回环。`responder` 默认 `lambda text: f"echo:{text}"`。
- `name`、`card()`(`{"name","transport":"offline-loopback","skills":["echo"]}`)、`send(task) -> A2AResult`。

### `class A2AAgentAdapter`
`A2AAgentAdapter(remote: A2AAgent, *, task_id: str = "task", name: str | None = None)`
- 实现 `Agent` 协议。`name` = 构造参数 `name`,缺省为构造时对 `remote.name` 的一次快照(之后对端改名
  不影响 provenance / trace)。`step(task, *, trace=None) -> AgentResult`:把 `task` 包成 `A2ATask` 交给
  `remote.send`,把 `A2AResult` 转成 `AgentResult`;输出文本原样继承自 remote,但标为数据(`TaskText`)。

### `a2a_agents`
`Registry[A2AAgent]`(seam 名 `a2a_agent`)。`make(spec, **kw)` / `names()`。
- 已注册:`"offline"`(→ `OfflineA2AStub`)、`"real"`(**占位、尚未实现**:缺 `[a2a]` extra 抛 `ImportError`,装了 extra 也必抛 `SeamError`)。

### `load_a2a_sdk() -> Any`
延迟 import 真实 A2A SDK(`a2a-sdk`,import 名 `a2a`);未装 `[a2a]` extra 给友好报错。

---

## llm provider 适配器(对外统一 OpenAI ChatCompletion 形状)

所有适配器都实现 corespine 的 `LLMProvider` 协议:
`chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None) -> ChatCompletion`。
传 OpenAI 形状的 messages / function-tools,拿回 OpenAI 形状的 `ChatCompletion`。`client=` 可注入
fake / 真实 client 做离线单测;不注入则在构造时经对应 `load_*_sdk()` 延迟 import 真实 SDK。

### `class OpenAICompatProvider`
`OpenAICompatProvider(model: str, *, max_tokens: int = 4096, client: Any = None, base_url: str | None = None, extra: dict[str, Any] | None = None, **client_kwargs)`
- 走官方 `openai` SDK 的 `chat.completions.create`。`model` 必填(各兼容端点模型名不同)。`base_url`
  指向兼容端点(留空 = 官方 OpenAI)。messages / tools 直传(本就是 OpenAI 形状)。extra `[openai]`。
- 一个适配器覆盖一切「OpenAI 兼容」端点(OpenAI / Azure / Together / Groq / DeepSeek / Mistral / xAI /
  Qwen / Moonshot / Ollama / vLLM / OpenRouter / LiteLLM …)。

### `class AnthropicProvider`
`AnthropicProvider(*, model: str = "claude-opus-4-8", max_tokens: int = 4096, client: Any = None, extra: dict[str, Any] | None = None, **client_kwargs)`
- 走官方 `anthropic` SDK 的 `messages.create`。内部把 OpenAI messages/tools 转成 Anthropic 原生形状,
  再把响应(text / tool_use / stop_reason / usage)转回 OpenAI `ChatCompletion`。extra `[anthropic]`。

### `class CohereProvider`
`CohereProvider(*, model: str = "command-r-plus", client: Any = None, extra: dict[str, Any] | None = None, **client_kwargs)`
- 走官方 `cohere` SDK 的 `ClientV2.chat`。Cohere v2 native → OpenAI `ChatCompletion`。extra `[cohere]`。

### `class GeminiProvider`
`GeminiProvider(*, model: str = "gemini-2.5-flash", client: Any = None, extra: dict[str, Any] | None = None, **client_kwargs)`
- 走官方 `google-genai` SDK 的 `models.generate_content`。Gemini native → OpenAI `ChatCompletion`
  (自造 tool_call id、args→JSON 串、role model→assistant)。同覆盖 AI Studio 与 Vertex。extra `[gemini]`。

### `class BedrockConverseProvider`
`BedrockConverseProvider(model: str, *, client: Any = None, region_name: str | None = None, extra: dict[str, Any] | None = None, **client_kwargs)`
- 走 `boto3` 的 `bedrock-runtime` Converse API(跨模型同形)。`model` 必填(= Bedrock modelId)。
  Converse native → OpenAI `ChatCompletion`。extra `[bedrock]`。

### `class FailoverProvider` / `StreamingFailoverProvider` / `make_failover_provider`
`FailoverProvider(providers: Sequence[LLMProvider], *, cooldown_seconds: float = 30.0, retryable_errors: tuple[type[BaseException], ...] = (ProviderError,), now_fn: Callable[[], float] = time.monotonic, failover_policy: Callable[[BaseException], FailoverDecision] = default_failover_policy)`
- 组合式容错 provider:包裹一组下游,做 (a) 轮询分摊 + (b) 失败后按分类冷却出错的那一家 +
  (c) 全冷却时强制按序回退,全失败抛 `FailoverExhaustedError`(聚合各下游原因,经凭据脱敏,绝不含 key)。
- 只 catch `retryable_errors`(默认 `ProviderError`),交给 `failover_policy` 给出
  `FailoverDecision(fallback: bool, cooldown: bool, suspect_request: bool = False)`;逻辑错(KeyError/ValueError…)照常上抛,不被吞。缺省
  `default_failover_policy`(保守,可注入替换):瞬时故障(网络 / 超时 / 408 / 425 / 429 / 5xx)→ 回退 + 冷却该家;
  401 / 402 / 403 / 404 或消息含余额 / 配额 / 计费 / 模型不存在 / 鉴权 → 回退 + **只冷却出错的那一家**;上下文超长
  与其它无法判定的 4xx → 疑似请求本身有问题(`FailoverDecision.suspect_request=True`):不冷却,**最多再试 1 家**,
  那家也以同类 4xx 拒绝则抛 `BadRequestProviderError`(一条畸形请求最多 2 次计费调用),以别的错误失败则停止并抛
  `FailoverExhaustedError`;`BadRequestProviderError` → 不回退、不冷却,直接上抛。游标 / 冷却表加锁,可跨线程共享。
- 不在顶层导出:`from spineagent.llm.failover_provider import FailoverProvider, FailoverDecision, default_failover_policy, make_failover_provider`。

### `ProviderError` / `NonRetryableProviderError` / `BadRequestProviderError` / `provider_error_from`
- `ProviderError` 来自 corespine(顶层再导出)。`retryable` 只表示「对**同一家**重试有没有意义」;「换另一家
  是否可能成功」由 `FailoverProvider` 的 `failover_policy` 决定。各适配器经
  `spineagent.llm.errors.provider_error_from(message, exc)` 归一 vendor 异常:网络 / 超时 / 408 / 425 / 429 /
  5xx / 取不到状态码 → `ProviderError(retryable=True)`;其余 4xx(400 / 401 / 403 / 404 / 413 / 422 …)→
  `NonRetryableProviderError`(code `provider.non_retryable`,`retryable=False`)。状态码进 `context["status"]`。
- `BadRequestProviderError(NonRetryableProviderError)`(code `provider.bad_request`):确知请求本身畸形、换谁都
  不行。来源:`provider_error_from` 遇到 400 / 422 且 vendor 错误体的稳定字段明确指出参数校验失败时(目前只有
  OpenAI 风格错误体:`type == "invalid_request_error"` 且带 `param`、`code` 不是上下文超长 / 模型 / 配额类;其它家
  无法据稳定字段区分「参数错」与「上下文超长」,保守地不判定);`FailoverProvider` 连续两家同类 4xx 时;自定义分类。
- `make_failover_provider(providers, **kw) -> FailoverProvider`:诚实选类——【全下游都实现
  `StreamingLLMProvider`】才返回带 `stream_chat` 的 `StreamingFailoverProvider`,混编则返回不带
  `stream_chat` 的基类(`isinstance(p, StreamingLLMProvider)` 如实报 False)。`now_fn` 可注入以确定性测试冷却。

### `llm_providers`
`Registry[LLMProvider]`(seam 名 `llm`)。`make(spec, **kw)` / `names()`。
- 已注册:`"mock"`(→ `corespine.MockProvider`)、`"openai"`、`"anthropic"`、`"cohere"`、`"gemini"`、`"bedrock"`、
  `"failover"`(组合 provider:`make("failover", downstreams=[{"spec": "openai", "model": …}, {"spec": "anthropic"}], cooldown_seconds=30)`——
  `downstreams` 是 spec 列表,经本注册表逐个构造成下游;也可传已构造的 `providers=[...]`)。

### `load_*_sdk()`
`load_anthropic_sdk()` / `load_openai_sdk()` / `load_cohere_sdk()` / `load_gemini_sdk()` / `load_boto3_sdk()`
→ `Any`。各延迟 import 对应真实 SDK,缺对应 extra 时给「pip install spineagent[<extra>]」友好报错。

---

## 信任边界:指令 / 数据分通道(`spineagent.agent.trust`,ADR 0003)

- `class TaskText(str)`:`TaskText(text: str, spans: tuple[tuple[int, int], ...] = ())`。str 子类,额外携带
  `untrusted_spans`(不可信字符区间)。对只认 str 的代码完全透明。
- `untrusted(text: str) -> TaskText`:把整段标为数据。`compose(*parts: str) -> str`:按序拼接并保留各段区间
  (全可信退回 plain str)。`lines_with_trust(text) -> list[tuple[str, bool]]`:按行给出是否完全可信。
- 规则:plain str = 调用方直接给的**指令**。**本包每个 agent 的 `AgentResult.output` 在产出时就是数据**
  (整段不可信的 `TaskText`);pipeline 上游输出、附件、`$prev` 回灌的工具结果、`AgentTool` / `McpClientTool` /
  `A2AAgentAdapter` 的返回、`SummaryMiddleware` 的摘要在边界再标一次(防第三方返回 plain str)。对 `TaskText`
  做普通 str 运算得到 plain str,标记会丢——实测会丢的有 `+` / `%` / `str.format` / f-string / `str.join` / `strip` / `replace` / `split` / 切片 / `upper` / `lower` / `str()` / `encode().decode()` / `string.Template` / JSON 往返(`copy` / `pickle` 保留);转手数据时用
  `untrusted` / `compose`。

---

## middleware 缝

### `class Middleware` (Protocol, runtime_checkable)
`before_step(self, ctx: StepContext) -> None`(就地改写 ctx);`after_step(self, ctx, result) -> AgentResult`(须保留 provenance)。

### `class StepContext`
`StepContext(agent: str, task: str, trace: TraceSink | None = None, step: int = 0, tools: list[str] = [], attachments: list[str] = [], extras: dict[str, Any] = {}, cleanups: list[Callable[[], None]] = [], inner_agent: Agent | None = None)`
- `tools` 只是**声明面**(给协作 middleware 用),不是执行闸。`cleanups`:before_step 登记的收尾回调,
  `MiddlewareAgent` 在本步结束时无论成败逆序**逐个**执行,一个失败不跳过其余的。`inner_agent`:被包裹的内层 agent(只读)。

### `class MiddlewareAgent`
`MiddlewareAgent(name: str, agent: Agent, middlewares: Iterable[Middleware])`
- 洋葱链:before 正序 → 内层 `agent.step(ctx.task, trace=ctx.trace)` → after 逆序 → provenance 重盖为本名。
  步序取号加锁(线程安全)。整步在调用方 `contextvars` 上下文的**副本**里跑:middleware 压进的作用域(审批
  配置、审批作用域)结构上只活在本步里,不会泄漏给同一线程里之后的请求。收尾失败的汇总:本步自身抛错时以本步的错误为准
  (收尾失败只作为 `__notes__` 附注,只含异常类型名);本步成功而收尾失败时,单个失败原样抛出、多个抛
  `ExceptionGroup`(`KeyboardInterrupt` 等非 `Exception` 优先)。

### 内置 middleware
- `TokenUsageMiddleware(tokenizer: Callable[[str], int] | None = None)`:记 token 计数(`.totals`)。
- `SummaryMiddleware(provider: LLMProvider | None = None, *, max_chars: int = 2000)`:task 超长时换成摘要(摘要标为数据)。
- `DynamicToolMiddleware(tools_by_step: Mapping[int, Sequence[str]] | None = None, *, default: Sequence[str] = ())`:按步写 `ctx.tools`。
- `AttachmentMiddleware(attachments: Mapping[str, str] | None = None)`:附件以数据段前置进 task。
- `middlewares`:`Registry[Middleware]`,已注册 `token_usage` / `summary` / `dynamic_tool` / `attachment` / `approval`。

---

## approval 缝(审批门 / Wait,ADR 0001 / 0002)

- `Decision`(StrEnum):`APPROVED` / `REJECTED` / `PENDING`。
- `ApprovalRequest(code, id, tool, arg_fingerprint="", arg_count=0, scope="", canonical_arguments="", preview=())`:
  定位摘要(code / id / 工具名 / schema 指纹 / 计数 / 作用域)+ 给审批人的内容。`canonical_arguments` 是完整规范化参数
  (键排序紧凑 JSON,正是 `id` 所哈希的内容),`arguments() -> dict` 解析出一份新 dict——**审批人据完整参数做决定**。
  `preview` 是列表展示用的 `(键, 值文本)` 元组:缺省不打码;长值只截断一次,注明「省略 N 字符」与该值的 sha256 前缀。
  `canonical_arguments` / `preview` 不进 `repr` / trace;`preview` 不参与相等比较。`prior_executions: int = 0`:门登记这条待审请求时,
  同一 request id 此前已被核销(执行)的次数(不进 id、不参与相等比较);>0 时 `preview` 首行注明「此前已执行过 n 次」。
- `make_approval_request(code, tool, arguments=None, *, nonce="", bind_values=False, scope="", sensitive_args=()) -> ApprovalRequest`:
  `bind_values=True` 时把完整规范化参数的 sha256 折进 `id`(参数一变即新请求);`scope` 折进 `id`;`sensitive_args`
  声明 preview 里要打码的参数路径(点号穿过 dict,如 `"password"`、`"body.to_token"`;显示为 `***(sha256:<前缀>)`)。
- `ApprovalGate`(Protocol):`name: str`;`review(request) -> Decision`(纯查询,幂等)。
  `ConsumableApprovalGate`(Protocol):额外 `consume(request) -> bool`,执行闸在**执行前**核销一次放行额度(工具随后
  抛异常,这次批准也已用掉)。可核销的门会产生需要人工决议的待审请求,**必须有显式作用域**。
- `AutoApprovalGate(*, allow=(), deny=(), default=Decision.APPROVED)`:工具名 glob 策略表,deny > allow > default,
  永不 pending;不可核销(常驻策略放行);不需要作用域。
- `ManualApprovalGate(*, token_store=None, max_requests=1024, max_pending_per_scope=64, request_ttl=3600.0, max_decided=4096, decided_ttl=600.0, now_fn=time.monotonic)`:
  `review` 登记待审;每个作用域最多 `max_pending_per_scope` 条待审、全表最多 `max_requests` 条,到上限时**拒绝新请求**
  (抛 `ApprovalGateError`,fail-closed),绝不淘汰既有的待审 / 已批准条目。`resolve(request_id, decision, *, uses=1,
  ttl_seconds=None) -> str` 只接受**已登记、未过期**的请求(否则 `UnknownApprovalRequest`),返回一次性 resume token;
  `uses=1` 缺省只放行一次、`uses=N` 放行 N 次、`uses=None` 为显式可选的幂等模式(有效期内不限次,有重放风险);
  `consume(request) -> bool`(请求须与登记时逐字段相等,含完整参数);`redeem(token) -> ResumeTicket`(重放抛
  `InvalidResumeToken`);`pending() -> list[ApprovalRequest]`(带完整参数与 preview);`executed_count(request_id) -> int`(此前已核销次数,
  `max_decided` 条 / `decided_ttl` 秒的有界提示性计数,超出丢最早的)。
- `ResumeTokenStore` / `InMemoryResumeTokenStore(*, max_tokens=1024, ttl=3600.0, now_fn=time.monotonic)` /
  `ResumeTicket(request_id, decision)`:ticket 只是 resume 句柄,不是执行凭据;批准在执行点核销,重放 ticket 不会多执行。
  token 存储有界(超出时最早签发的未兑现 token 失效)、过期、加锁,兑现即删除。
- `approval_scope(scope: str)`(上下文管理器)/ `current_approval_scope() -> str | None`:设置 / 读取当前上下文的审批作用域。
  **作用域是调用方给的不透明字符串,必须在共享同一个门的所有调用方之间唯一**(建议 租户 id + 会话 id);同作用域 + 同工具
  + 同参数就是同一个请求、共用同一个批准(定义内行为)。
- `preflight_tool_approvals(calls: Sequence[tuple[str, Mapping, object | None]], *, available=()) -> list[ApprovalError | None]`:
  执行前预检一批调用(只 review、不核销;待审的被登记),返回与输入对齐的「错误或 None」。
- `ApprovalMiddleware(gate, *, gated_tools=(), code="tool_call", scope=None, sensitive_args=None, strict_names=False)`:
  before_step 把审批配置与作用域压进当前上下文(`ctx.cleanups` 弹出);作用域内**每一次真实工具调用**都按「工具名 +
  规范化参数 + 作用域」review 并核销。作用域:`scope=` > 外层 `approval_scope`;**门可核销而两者都没有时,before_step
  在内层 agent 运行前抛 `ApprovalConfigError`**(不生成隐式作用域);同步门不需要。`sensitive_args`:`{工具名: [参数路径]}`。
  `gated_tools` 须是确切名字:含通配符 / 空名在构造时抛 `ApprovalConfigError`;能推断被包裹 agent 的工具清单时,只差
  大小写 / 分隔符的名字抛 `ApprovalConfigError`,找不到的名字发一次 `UserWarning`(`strict_names=True` 时改为报错)。
  缺省 `gated_tools` 空 = 零行为变化。每次受审批调用恰好一条 `mw_approval` trace(只记 code / 计数 / 决议)。
- `enforce_tool_approval(tool: str, arguments, *, target=None, available=()) -> None`:执行点在调用工具前调它;
  approved(并核销)返回,rejected 抛 `ApprovalRejected`,pending 抛 `ApprovalPending`(`context["request_id"]` /
  `context["scope"]`),门抛异常或返回非 `Decision` 抛 `ApprovalGateError`(fail-closed;门自己抛的 `ApprovalError`
  原样上抛)。`target` = 被执行的工具对象(自带 `require_approval` 闸时同一个门不重复审);`available` = 本执行点
  已注册工具名(检测近似名写错)。
- `require_approval(tool, gate, *, code="tool_call", scope=None, sensitive_args=()) -> FunctionTool | Tool`:把闸绑进工具
  对象本身,不依赖上下文(裸线程 / 第三方 agent / 别名注册都绕不过)——**安全场景首选**。`scope` 不给时取**执行时**外层的
  `approval_scope`(模块级共享的工具对象就这么用);两者都没有而门可核销时,调用时抛 `ApprovalConfigError`、不执行。
- `spineagent.tools.tool.reachable_tool_names(obj) -> frozenset[str] | None`:agent / 工具在本地能执行到的工具名(经可选
  `tool_inventory()`);推断不了返回 `None`。
- 错误:`ApprovalError`(基类)/ `ApprovalRejected`(`approval.rejected`,不可重试)/ `ApprovalPending`
  (`approval.pending`,可重试)/ `ApprovalGateError`(`approval.gate_error`)/ `ApprovalConfigError`(`approval.config_error`,
  也是 `ValueError`)/ `UnknownApprovalRequest`(`approval.unknown_request`)/ `ApprovalConflict` / `InvalidResumeToken`。
- 批准语义(ADR 0002 决策 4):缺省一次性消费、绑定显式作用域、只能批准已存在的请求;恢复 = 批准后在同一作用域里重跑
  这个 run——**至少一次**:此前已执行过的工具会再次执行(工具应幂等;不想重跑就用 `on_approval="feed_back"`)。
- `approval_gates` / `make_approval_gate(spec, **kw)`:内置 `auto` / `manual`。

---

## artifact 缝

- `Artifact(name: str, data: bytes, mime: str = "application/octet-stream", producer: str = "")`;`Artifact.from_text(name, text, *, mime="text/plain", producer="")`。
- `ArtifactRef(key, sink, name, mime, producer, size)`:轻量引用,挂在 `AgentResult.artifacts`。
- `ArtifactSink`(Protocol):`name`、`store(artifact) -> ArtifactRef`、`fetch(ref) -> Artifact`。
- `InProcessArtifactSink()`、`BlobArtifactSink(store: BlobStore, *, name="blob")`、`artifact_sinks` Registry。

---

## sandbox 缝

- `Limits(timeout_seconds: float | None = None, max_output_chars: int | None = None, max_ops: int | None = None, max_memory_bytes: int | None = None)`;
  `DEFAULT_LIMITS = Limits(5.0, 64_000, 100_000, 32 * 2**20)`。
- `SandboxResult(sandbox, output, returncode=0, usage=ResourceUsage(), error=None)`,`ok` = `returncode == 0`;
  `error` 为 `disallowed` / `limit_exceeded` / `syntax` / `error`。`ResourceUsage(ops, output_chars, wall_seconds, memory_bytes)`。
- `InProcessSandbox(*, clock: Callable[[], float] = time.monotonic)`;`run(code, *, timeout=None, limits=None, env=None) -> SandboxResult`:
  受限白名单表达式求值器(无 Import / Attribute / 任意调用),工作量预算、值大小上限、输出上限;`timeout`
  为**协作式 deadline**(每个节点前后各查一次时钟,超时判 `limit_exceeded`)。协作式超时**无法中断单个内建
  调用**:单次求值的代价上界来自**先验规模守卫**(整数位数 / 文本长度 / 容器元素数 / 嵌套深度上限;幂、序列
  重复与拼接按结果规模预判;`round` 的 `|ndigits|` ≤ 2467;`int()` 的数字串 ≤ 4300 字符;`sum` 只做数值累加)
  、**工作量预算**(`max_ops`:节点 1 单位,大操作按输入 / 结果规模折算,如 `sorted(x)` 记 `len(x)`、文本按存储字节
  折算;`max_ops=None` 时仍有 100 万单位的硬上限)与**内存预算**(`max_memory_bytes`:每个节点产生的值按估算字节——
  `sys.getsizeof` 口径,宽字符串按实际宽度,容器递归——在产生的那一刻累加并检查,容器字面量 / 调用实参逐个元素求值时
  就会停下;缺省 32 MiB,`None` 时仍有 128 MiB 的硬上限;累计口径,不随临时值释放回退)。`ResourceUsage.ops` /
  `memory_bytes` 即消耗的工作量单位 / 估算字节。`MemoryError` / `RecursionError`(含解析器栈溢出)一律容住为
  `limit_exceeded`。支持 `{**d}`(只展开 dict);调用里的 `**` 展开明确拒绝(`disallowed`);长的左结合链(`1+…+n`)
  迭代求值。
- `sandboxes` Registry:`in_process`;`subprocess` / `container` 是**占位、尚未实现**(`subprocess` 必抛
  `SeamError`;`container` 缺 `[sandbox]` extra 抛 `ImportError`,装了也必抛 `SeamError`)。`load_container_sdk()`。

---

## skills 缝

- `SkillSpec(name, description, inputs: dict)`;`Skill`(Protocol):`spec`、`describe() -> dict`、`invoke(args) -> SkillResult`。
- `FixtureSkill(spec, script, *, sandbox=None)`:脚本经 Sandbox 执行,失败抛 `SkillError(skill, reason, detail)`。
- `SkillBundle.load(path, *, sandbox=None) -> Skill`:目录(manifest.toml + 脚本)加载器;`skill_registry`;`skill_as_function_tool(skill) -> FunctionTool`
  (在 `FunctionCallingAgent` 里,`SkillError` 会按工具失败路径喂回模型)。

---

## deep research

- `DeepResearchAgent(name="deep_research", *, provider=None, tools=(), planner=None, max_subqueries=5, retriever_system="", synthesis_system="")`:
  planner 分解 → `Coordinator.run_parallel` 并行检索(`FunctionCallingAgent`)→ `LlmAgent` 综合。`usage` 为全部检索 +
  综合的累加。审批挂起 / 拒绝
  不会被吞成失败发现,而是原样上抛。重名工具构造即抛 `ValueError`。`default_planner(task) -> list[str]`。

---

## conformance(本包绑定的不变量)

供 `corespine.ConformanceSuite(implementations, pack)` 消费;`pack` 即下列 `InvariantPack`。

- `AGENT_INVARIANTS: InvariantPack[Agent]`(名 `agent_step`):`step_returns_output`、
  `result_carries_agent_provenance`、`step_traces_are_privacy_safe`。
- `TOOL_INVARIANTS: InvariantPack[Tool]`(名 `tool_call`):`result_carries_tool_provenance`、`run_returns_output`。
- `POLICY_INVARIANTS: InvariantPack[ToolPolicy]`(名 `tool_policy`):`action_is_a_known_variant`、
  `never_calls_an_unavailable_tool`、`empty_tools_yields_nonempty_finish`、`decide_is_pure`、
  `untrusted_data_is_never_an_instruction`。
- `LLM_INVARIANTS` / `STREAMING_INVARIANTS`:OpenAI 形状 / finish_reason 取值域 / usage 非负 /
  tool_call 往返;流式各块形状 + 流式拼接 == 非流式。
- `SANDBOX_INVARIANTS`:provenance / 产出非空 / 记账非负 / 上限生效 / 无网络出口 / `timeout_takes_effect` /
  `amplifying_expression_is_bounded`(值放大型表达式必须在超时量级内被拒 / 被终止)/ `memory_is_bounded`(超出
  `max_memory_bytes` 时在上限附近判 `limit_exceeded`,不先全部物化)。
- `SKILL_INVARIANTS`、`MIDDLEWARE_INVARIANTS`、`ARTIFACT_INVARIANTS`、`APPROVAL_INVARIANTS`。
- `APPROVAL_ENFORCEMENT_INVARIANTS: InvariantPack[ToolExecutionHarness]`(名 `approval_enforcement`):
  `unapproved_gated_tool_never_executes`、`pending_gate_requires_explicit_scope`、`rerun_does_not_bypass`、
  `changed_arguments_require_reapproval`、
  `approval_is_consumed_once`、`approval_is_scope_bound`、`gate_failure_blocks_execution`、
  `ungated_tools_are_unaffected`——全部用带副作用计数的真实工具函数断言。
- `TOOL_TRACE_INVARIANTS: InvariantPack[ToolExecutionHarness]`(名 `tool_trace`):`unknown_tool_name_is_not_traced`。
- `ToolExecutionHarness`(Protocol):`run(calls, tools, *, gate, gated_tools=(), trace=None, scope=None) -> None`,把一串
  `(工具名, 参数值)` 交给某个会执行工具的 agent 真实跑一次;`ScriptedToolCallProvider(calls, *, final="done", usage=None)`:
  离线脚本化 provider,按对话中 assistant 条数回放 tool_calls(无状态、可重放、线程安全),供 harness 驱动
  `FunctionCallingAgent`。二者在 `spineagent.conformance`。

### `__version__`
`spineagent.__version__ -> str`:取自已安装包的元数据(`importlib.metadata.version("spineagent")`),
随发行版变化;以 `pyproject.toml` 的 `version` 为准(编写本文时为 `"0.3.1"`)。
