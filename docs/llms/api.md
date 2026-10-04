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
`AgentResult(agent: str, output: str, usage: dict[str, int] | None = None, error: dict[str, object] | None = None, artifacts: tuple[ArtifactRef, ...] = ())`
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
`FunctionCallingAgent(name: str, model: LLMProvider, tools: Iterable[FunctionTool], *, system: str = "", max_steps: int = 8, fail_fast: bool = False, include_error_message: bool = False)`
- `step(task, *, trace=None) -> AgentResult`:**真 LLM** 原生 function-calling 多步循环——把每个
  `FunctionTool.schema()` 喂给 `model.chat(messages, tools=...)`;模型回 `tool_calls` 则逐个
  `parse_arguments` 校验 → `enforce_tool_approval` 审批 → `invoke`、以 OpenAI `tool` 角色消息喂回、
  再 chat;无 tool_calls 则出文本收尾。触顶 `max_steps` 兜底非空。`usage` 为各轮累加。
- 工具失败:工具函数抛的 `Exception`(审批错误除外)归一成 tool 消息
  `error: tool failed [code=<code> type=<异常类型名>]` 喂回模型(`code` 为 CorespineError 的 code,否则
  `tool.execution_failed`);`include_error_message=True` 才附异常消息原文;`fail_fast=True` 恢复冒泡。
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
字符、嵌套深度 ≤ 100、幂指数绝对值 ≤ 10000、幂 / 乘法结果 ≤ 8192 位,越界立即抛 `ValueError`。

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
  `contextvars` 上下文的副本里跑(审批作用域随之生效)。`timeout`(整批,自调用起)/ `task_timeout`
  (单任务,自该任务开始跑起)缺省不限;到点未返回的任务以 `AgentTimeoutError`(code
  `orchestration.timeout`)归一的 `AgentResult.error` 返回,不挂住整批(挂死线程无法强杀,仍在后台)。
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
`FailoverProvider(providers: Sequence[LLMProvider], *, cooldown_seconds: float = 30.0, retryable_errors: tuple[type[BaseException], ...] = (ProviderError,), now_fn: Callable[[], float] = time.monotonic)`
- 组合式容错 provider:包裹一组下游,做 (a) 轮询分摊 + (b) 撞可重试错后冷却窗口内跳过 +
  (c) 全冷却时强制按序回退,全失败抛 `FailoverExhaustedError`(聚合各下游原因,经凭据脱敏,绝不含 key)。
- 只 catch `retryable_errors`(默认 `ProviderError`);逻辑错(KeyError/ValueError…)照常上抛,不被吞。
  `NonRetryableProviderError`(坏请求)**不回退、不冷却**,直接上抛。游标 / 冷却表加锁,可跨线程共享。
- 不在顶层导出:`from spineagent.llm.failover_provider import FailoverProvider, make_failover_provider`。

### `ProviderError` / `NonRetryableProviderError` / `provider_error_from`
- `ProviderError` 来自 corespine(顶层再导出)。各适配器经 `spineagent.llm.errors.provider_error_from(message, exc)`
  归一 vendor 异常:HTTP 400 / 413 / 422 → `NonRetryableProviderError`(code `provider.bad_request`,
  `retryable=False`);其余(网络 / 超时 / 408 / 429 / 5xx / 401 / 403 / 404 / 取不到状态码)→
  `ProviderError(retryable=True)`。状态码进 `context["status"]`。
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
- 规则:plain str = 调用方直接给的**指令**;pipeline 上游输出、附件、`$prev` 回灌的工具结果、`AgentTool` /
  `McpClientTool` / `A2AAgentAdapter` 的返回、`SummaryMiddleware` 的摘要都是**数据**。对 `TaskText` 做普通
  str 运算(`+` / f-string / `strip` / 切片)得到 plain str,标记会丢——转手数据时用 `untrusted` / `compose`。

---

## middleware 缝

### `class Middleware` (Protocol, runtime_checkable)
`before_step(self, ctx: StepContext) -> None`(就地改写 ctx);`after_step(self, ctx, result) -> AgentResult`(须保留 provenance)。

### `class StepContext`
`StepContext(agent: str, task: str, trace: TraceSink | None = None, step: int = 0, tools: list[str] = [], attachments: list[str] = [], extras: dict[str, Any] = {}, cleanups: list[Callable[[], None]] = [])`
- `tools` 只是**声明面**(给协作 middleware 用),不是执行闸。`cleanups`:before_step 登记的收尾回调,
  `MiddlewareAgent` 在本步结束时无论成败逆序执行。

### `class MiddlewareAgent`
`MiddlewareAgent(name: str, agent: Agent, middlewares: Iterable[Middleware])`
- 洋葱链:before 正序 → 内层 `agent.step(ctx.task, trace=ctx.trace)` → after 逆序 → provenance 重盖为本名。
  步序取号加锁(线程安全)。

### 内置 middleware
- `TokenUsageMiddleware(tokenizer: Callable[[str], int] | None = None)`:记 token 计数(`.totals`)。
- `SummaryMiddleware(provider: LLMProvider | None = None, *, max_chars: int = 2000)`:task 超长时换成摘要(摘要标为数据)。
- `DynamicToolMiddleware(tools_by_step: Mapping[int, Sequence[str]] | None = None, *, default: Sequence[str] = ())`:按步写 `ctx.tools`。
- `AttachmentMiddleware(attachments: Mapping[str, str] | None = None)`:附件以数据段前置进 task。
- `middlewares`:`Registry[Middleware]`,已注册 `token_usage` / `summary` / `dynamic_tool` / `attachment` / `approval`。

---

## approval 缝(审批门 / Wait,ADR 0001 / 0002)

- `Decision`(StrEnum):`APPROVED` / `REJECTED` / `PENDING`。
- `ApprovalRequest(code, id, tool, arg_fingerprint="", arg_count=0, scope="", preview=())`:定位摘要不含参数值;
  `scope` 是作用域(已折进 `id`);`preview` 是给审批人看的 `(键, 脱敏截断后的值)` 元组(不进 trace / repr / 相等比较)。
- `make_approval_request(code, tool, arguments=None, *, nonce="", bind_values=False, scope="", redact=None) -> ApprovalRequest`:
  `bind_values=True` 时把规范化参数值的 sha256 折进 `id`(参数一变即新请求);`scope` 折进 `id`;给 `redact` 才生成预览。
- `default_redactor(key, value) -> str`:键名命中敏感词表(password / secret / token / api_key / auth / cookie / session …)
  整值打码为 `***`,其余 repr / 紧凑 JSON 渲染并截断到 200 字符。脱敏钩子类型 `Redactor = Callable[[str, object], str]`。
- `ApprovalGate`(Protocol):`name: str`;`review(request) -> Decision`(纯查询,幂等)。
  `ConsumableApprovalGate`(Protocol):额外 `consume(request) -> bool`,执行闸在执行前核销一次放行额度。
- `AutoApprovalGate(*, allow=(), deny=(), default=Decision.APPROVED)`:工具名 glob 策略表,deny > allow > default,
  永不 pending;不可核销(常驻策略放行)。
- `ManualApprovalGate(*, token_store=None, max_requests=1024, request_ttl=3600.0, now_fn=time.monotonic)`:
  `review` 登记待审(满了先清过期再淘汰最早的);`resolve(request_id, decision, *, uses=1, ttl_seconds=None) -> str`
  只接受**已登记、未过期**的请求(否则 `UnknownApprovalRequest`),返回一次性 resume token;`uses=1` 缺省只放行一次、
  `uses=N` 放行 N 次、`uses=None` 为显式可选的幂等模式(有效期内不限次,有重放风险);`consume(request) -> bool`;
  `redeem(token) -> ResumeTicket`(重放抛 `InvalidResumeToken`);`pending() -> list[ApprovalRequest]`(带预览)。
- `ResumeTokenStore` / `InMemoryResumeTokenStore` / `ResumeTicket(request_id, decision, scope="")`:ticket 只是 resume
  句柄(告诉你在哪个作用域重跑),不是执行凭据;批准在执行点核销,重放 ticket 不会多执行。
- `approval_scope(scope: str)`(上下文管理器)/ `current_approval_scope() -> str | None`:设置 / 读取当前上下文的审批作用域。
- `ApprovalMiddleware(gate, *, gated_tools=(), code="tool_call", scope=None, redact=None)`:before_step 把审批配置与作用域
  压进当前上下文(`ctx.cleanups` 弹出);作用域内**每一次真实工具调用**都按「工具名 + 规范化参数 + 作用域」review 并核销。
  作用域:`scope=` > 外层 `approval_scope` > 缺省每次 step 新建(并传给嵌套 agent)。`gated_tools` 须是确切名字:含通配符 /
  空名在构造时抛 `ApprovalConfigError`;能推断被包裹 agent 的工具清单时,对应不到已知工具的名字在首个工具执行前抛
  `ApprovalConfigError`。缺省 `gated_tools` 空 = 零行为变化。
- `enforce_tool_approval(tool: str, arguments, *, target=None, available=()) -> None`:执行点在调用工具前调它;
  approved(并核销)返回,rejected 抛 `ApprovalRejected`,pending 抛 `ApprovalPending`(`context["request_id"]` /
  `context["scope"]`),门抛异常或返回非 `Decision` 抛 `ApprovalGateError`(fail-closed)。`target` = 被执行的工具对象
  (自带 `require_approval` 闸时同一个门不重复审);`available` = 本执行点已注册工具名(检测近似名写错)。
- `require_approval(tool, gate, *, code="tool_call", scope=None, redact=None) -> FunctionTool | Tool`:把闸绑进工具对象
  本身,不依赖上下文(裸线程 / 第三方 agent / 别名注册都绕不过)——**安全场景首选**。
- `spineagent.tools.tool.reachable_tool_names(obj) -> frozenset[str] | None`:agent / 工具在本地能执行到的工具名(经可选
  `tool_inventory()`);推断不了返回 `None`。
- 错误:`ApprovalError`(基类)/ `ApprovalRejected`(`approval.rejected`,不可重试)/ `ApprovalPending`
  (`approval.pending`,可重试)/ `ApprovalGateError`(`approval.gate_error`)/ `ApprovalConfigError`(`approval.config_error`,
  也是 `ValueError`)/ `UnknownApprovalRequest`(`approval.unknown_request`)/ `ApprovalConflict` / `InvalidResumeToken`。
- 批准语义(ADR 0002 决策 4):缺省一次性消费、绑定作用域、只能批准已存在的请求;恢复 = 在同一作用域里重跑 step
  (`with approval_scope(exc.context["scope"]): agent.step(task)`)。
- `approval_gates` / `make_approval_gate(spec, **kw)`:内置 `auto` / `manual`。

---

## artifact 缝

- `Artifact(name: str, data: bytes, mime: str = "application/octet-stream", producer: str = "")`;`Artifact.from_text(name, text, *, mime="text/plain", producer="")`。
- `ArtifactRef(key, sink, name, mime, producer, size)`:轻量引用,挂在 `AgentResult.artifacts`。
- `ArtifactSink`(Protocol):`name`、`store(artifact) -> ArtifactRef`、`fetch(ref) -> Artifact`。
- `InProcessArtifactSink()`、`BlobArtifactSink(store: BlobStore, *, name="blob")`、`artifact_sinks` Registry。

---

## sandbox 缝

- `Limits(timeout_seconds: float | None = None, max_output_chars: int | None = None, max_ops: int | None = None)`;
  `DEFAULT_LIMITS = Limits(5.0, 64_000, 100_000)`。
- `SandboxResult(sandbox, output, returncode=0, usage=ResourceUsage(), error=None)`,`ok` = `returncode == 0`;
  `error` 为 `disallowed` / `limit_exceeded` / `syntax` / `error`。`ResourceUsage(ops, output_chars, wall_seconds)`。
- `InProcessSandbox(*, clock: Callable[[], float] = time.monotonic)`;`run(code, *, timeout=None, limits=None, env=None) -> SandboxResult`:
  受限白名单表达式求值器(无 Import / Attribute / 任意调用),工作量预算、值大小上限、输出上限;`timeout`
  为**协作式 deadline**(每个节点前后各查一次时钟,超时判 `limit_exceeded`)。协作式超时**无法中断单个内建
  调用**:单次求值的代价上界来自**先验规模守卫**(整数位数 / 文本长度 / 容器元素数 / 嵌套深度上限;幂、序列
  重复与拼接按结果规模预判;`round` 的 `|ndigits|` ≤ 2467;`int()` 的数字串 ≤ 4300 字符;`sum` 只做数值累加)
  与**工作量预算**(`max_ops`:节点 1 单位,大操作按输入 / 结果规模折算,如 `sorted(x)` 记 `len(x)`;
  `max_ops=None` 时仍有 100 万单位的硬上限)。`ResourceUsage.ops` 即消耗的工作量单位。
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
  planner 分解 → `Coordinator.run_parallel` 并行检索(`FunctionCallingAgent`)→ `LlmAgent` 综合。审批挂起 / 拒绝
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
  `amplifying_expression_is_bounded`(值放大型表达式必须在超时量级内被拒 / 被终止)。
- `SKILL_INVARIANTS`、`MIDDLEWARE_INVARIANTS`、`ARTIFACT_INVARIANTS`、`APPROVAL_INVARIANTS`。
- `APPROVAL_ENFORCEMENT_INVARIANTS: InvariantPack[ToolExecutionHarness]`(名 `approval_enforcement`):
  `unapproved_gated_tool_never_executes`、`rerun_does_not_bypass`、`changed_arguments_require_reapproval`、
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
