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
`AgentResult(agent: str, output: str, usage: dict[str, int] | None = None, error: dict[str, object] | None = None)`
- frozen dataclass。一次 agent 步的结果。`agent` = provenance(产出它的 agent 名)。
- 属性 `ok -> bool`:`self.error is None`。
- 契约:成功路径 `error is None`;`error` 仅由编排层弹性模式(`Coordinator(..., resilient=True)`)
  捕获 `step` 异常时填充,值是 `corespine.errors.error_to_dict(exc)` 的归一 dict(含 `code` /
  `retryable` / `message` / `context`)。

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
- `$prev`:工具参数里的字面量 `$prev` 在执行前替换为上一步观测输出(首步无上一步则替换为空串)。
- `max_steps` = **最多调用多少次工具**(收尾不占预算);触顶强制收尾,绝不死循环。
- 实现 `Agent` 协议,可直接进 `Coordinator` / `ChainAgent` / 被 `AgentTool` 包成工具。

### `class FunctionCallingAgent`
`FunctionCallingAgent(name: str, model: LLMProvider, tools: Iterable[FunctionTool], *, system: str = "", max_steps: int = 8)`
- `step(task, *, trace=None) -> AgentResult`:**真 LLM** 原生 function-calling 多步循环——把每个
  `FunctionTool.schema()` 喂给 `model.chat(messages, tools=...)`;模型回 `tool_calls` 则逐个
  `tool.invoke(json.loads(arguments))`、以 OpenAI `tool` 角色消息喂回、再 chat;无 tool_calls 则
  出文本收尾。触顶 `max_steps` 兜底非空。
- 离线 `MockProvider` 不回 tool_calls → 直接出文本(诚实:离线不假装会 function-calling)。要真正
  跑工具循环,需注入会回 `tool_calls` 的 provider(真实后端,或测试用脚本化 fake)。

### `class AgentTool`
`AgentTool(agent: Agent, *, name: str | None = None)`
- 实现 `Tool` 协议。`name` 默认取 `agent.name`。
- `run(arg: str) -> ToolResult`:对子 agent 跑一步,把 `output` 包成带 provenance 的 `ToolResult`。
- 用于分层 / 督导式多 agent(可层层嵌套)。最薄桥:只搬运文本,子 agent 的 usage / error 不透传;
  子 agent 抛异常照常上抛(错误处理归编排层 / 调用方)。

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
`SyntaxToolPolicy()` — 离线确定性默认实现。`decide(...)` 按任务文本里 `<tool>: <arg>` 显式语法 +
工具名集合确定性路由:游标 = `len(history)`,第 cursor 条工具指令尚存则 `ToolCall(该行工具名, 该行参数)`,
指令耗尽则 `Finish`(把非指令正文行 + 最后一步观测拼成非空答案)。**不**假装 LLM 推理。

### `tool_policies`
`Registry[ToolPolicy]`(seam 名 `tool_policy`)。
- `tool_policies.make(spec, **kwargs) -> ToolPolicy`、`tool_policies.names() -> list[str]`。
- 已注册:`"offline"`(→ `SyntaxToolPolicy`)、`"llm"`(真实推理式占位,**调用即抛 `SeamError`**——
  留待接真 provider 解析 function-calling 后接入)。

---

## tools

### `class ToolResult`
`ToolResult(tool: str, output: str)` — frozen dataclass。`tool` = provenance(产出它的工具名)。

### `class Tool` (Protocol, runtime_checkable)
- 属性 `name: str`;方法 `run(self, arg: str) -> ToolResult`。

### `class EchoTool`
`EchoTool()`,`name = "echo"`。`run(arg) -> ToolResult`:原样回显 `arg`。

### `class CalcTool`
`CalcTool()`,`name = "calc"`。`run(arg) -> ToolResult`:安全求值算术表达式(白名单 `+ - * / % **`
与一元 `+ -`,整数结果去掉 `.0`);非算术节点抛 `ValueError`,绝不 eval 任意代码。

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
- `run_parallel(task: str, *, max_workers: int | None = None, resilient: bool = False) -> list[AgentResult]`:
  线程池并发跑同一任务,结果仍按 agent 输入顺序返回(`max_workers` 默认 = agent 数)。
- `run_pipeline(task: str, *, resilient: bool = False) -> list[AgentResult]`:链式——上一个 agent 的
  `output` 作下一个的输入,保序收集每段。
- 弹性容错 `resilient=True`:单 agent 异常归一为 `error_to_dict(exc)` 塞进该步 `AgentResult.error`,
  批次继续(顺序 / 并行跑完其余;流水线在失败处停止)。`resilient=False`(默认)= fail-fast,异常冒泡。
- 编排级 trace 只记 `mode` / `agent_count` / `failures` / `took_ms`,绝不记正文。

### `class ChainAgent`
`ChainAgent(name: str, agents: Iterable[Agent])`
- 实现 `Agent` 协议。`step(task, *, trace=None) -> AgentResult`:复用 `Coordinator(...).run_pipeline(task)`
  把任务逐段传递,返回末端 agent 输出(provenance = chain 名;空链退化为恒等透传)。失败 fail-fast 冒泡。
- 让流水线成为一等可组合单元:可进 `Coordinator` / 当 `AgentTool` 工具 / 套进另一个 chain。

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
- 最薄阻抗匹配:只做 str 单参 + 单键结果映射。

### `mcp_clients`
`Registry[McpClient]`(seam 名 `mcp_client`)。`make(spec, **kw)` / `names()`。
- 已注册:`"offline"`(→ `OfflineMcpStub`)、`"real"`(走 `[mcp]` extra 延迟 import 后抛 `SeamError`,待接入)。

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
`A2AAgentAdapter(remote: A2AAgent, *, task_id: str = "task")`
- 实现 `Agent` 协议。`name` 取 `remote.name`。`step(task, *, trace=None) -> AgentResult`:把 `task`
  包成 `A2ATask` 交给 `remote.send`,把 `A2AResult` 转成 `AgentResult`。透明桥:输出原样继承自 remote。

### `a2a_agents`
`Registry[A2AAgent]`(seam 名 `a2a_agent`)。`make(spec, **kw)` / `names()`。
- 已注册:`"offline"`(→ `OfflineA2AStub`)、`"real"`(走 `[a2a]` extra 延迟 import 后抛 `SeamError`,待接入)。

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

### `llm_providers`
`Registry[LLMProvider]`(seam 名 `llm`)。`make(spec, **kw)` / `names()`。
- 已注册:`"mock"`(→ `corespine.MockProvider`)、`"openai"`、`"anthropic"`、`"cohere"`、`"gemini"`、`"bedrock"`。

### `load_*_sdk()`
`load_anthropic_sdk()` / `load_openai_sdk()` / `load_cohere_sdk()` / `load_gemini_sdk()` / `load_boto3_sdk()`
→ `Any`。各延迟 import 对应真实 SDK,缺对应 extra 时给「pip install spineagent[<extra>]」友好报错。

---

## conformance(本包绑定的不变量)

供 `corespine.ConformanceSuite(implementations, pack)` 消费;`pack` 即下列 `InvariantPack`。

- `AGENT_INVARIANTS: InvariantPack[Agent]`(名 `agent_step`):`step_returns_output`、
  `result_carries_agent_provenance`、`step_traces_are_privacy_safe`。
- `TOOL_INVARIANTS: InvariantPack[Tool]`(名 `tool_call`):`result_carries_tool_provenance`、`run_returns_output`。
- `POLICY_INVARIANTS: InvariantPack[ToolPolicy]`(名 `tool_policy`):`action_is_a_known_variant`、
  `never_calls_an_unavailable_tool`、`empty_tools_yields_nonempty_finish`、`decide_is_pure`。

### `__version__`
`spineagent.__version__ -> str`(当前 `"0.0.3"`)。
