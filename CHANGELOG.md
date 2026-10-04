# Changelog

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### Security

- 审批门改为工具调用点上的强制闸(ADR 0002):`FunctionCallingAgent` / `ToolUsingAgent` 在每一次
  真实调用工具前按「真实工具名 + 规范化参数」向审批门 review;未批准不执行。中间件组合顺序、嵌套
  agent、`AgentTool`、`ChainAgent`、`Coordinator.run_parallel`、`DeepResearchAgent` 下均生效;
  审批门故障时 fail-closed。
- `InProcessSandbox`:消除热路径上被 beartype claw 每次调用重新装饰的嵌套函数(大 env 求值从秒级
  降到毫秒级);`timeout` 作为协作式 deadline 在节点之间检查(超时判 `limit_exceeded`)。注:这一条
  **不能**中断单个内建调用,单次求值的代价上界见下一条。
- `InProcessSandbox` 的代价上界改由**先验规模守卫**保证(审查复现 `round(1, -10**7)` 在一个节点内部跑了
  10 秒且返回成功):`round` 的 `|ndigits|` ≤ 2467、`int()` 的数字串 ≤ 4300 字符、`sum` 只做数值累加、
  值的嵌套深度 ≤ 100;每个节点**之后**也检查 deadline;新增总工作量预算(`max_ops` 计工作量单位,单节点内
  的大操作按输入 / 结果规模折算,`max_ops=None` 时仍有 100 万单位硬上限),反复引用同一个大值的写法被提前
  截住。深嵌套 env 不再让 `run()` 抛 `RecursionError`。
- `CalcTool`:表达式长度 ≤ 4096、嵌套深度 ≤ 100,幂 / 乘法结果位数复用 sandbox 的昂贵二元运算
  守卫;越界立即抛 `ValueError`。
- 审批的批准语义改为安全边界(ADR 0002 修订):批准**缺省一次性消费**(执行时核销;`resolve(uses=N)`
  放行 N 次,`uses=None` 为显式可选的旧幂等模式);请求绑定调用方作用域(`approval_scope` /
  `ApprovalMiddleware(scope=)` / `require_approval(scope=)`,缺省每次 step 一个新作用域),A 的批准对 B 无效;
  `ManualApprovalGate.resolve` 只接受已登记、未过期的请求(不能离线算出 id 预先批准);请求表有上限与过期;
  `pending()` 的条目带脱敏 + 截断的参数预览(只给审批人,不进 trace);受审批工具名含通配符 / 对应不到已知工具 /
  与已注册工具仅大小写或分隔符不同时 fail-closed(`ApprovalConfigError`)。
- 等待审批期间的重跑不再重放其它工具的副作用(ADR 0002 决策 5a):`ApprovalMiddleware` 的步内记账让因审批挂起而在
  同一作用域重跑时复用已执行调用的结果(含并行分支);`FunctionCallingAgent` 缺省**先审后行**——一轮 tool_calls
  里有任何受审批调用未获批则整轮不执行;新增不中断模式 `on_approval="feed_back"` 与预检 API
  `preflight_tool_approvals`。
- `MiddlewareAgent`:收尾回调逐个执行、各自捕获(一个失败不再跳过其余的),整步在调用方上下文的副本里跑——
  自定义中间件的 cleanup 抛异常不再让审批作用域 / trace 落点泄漏到同一线程之后的请求。新增 `bind_context`,
  把当前上下文带进调用方自建的线程(动态作用域本身不跨线程)。
- 指令 / 数据分通道(ADR 0003):pipeline 上游输出、附件、`$prev` 回灌的工具结果、`AgentTool` /
  `McpClientTool` / `A2AAgentAdapter` 的返回都标为数据(`TaskText`),`SyntaxToolPolicy` 不再把其中的
  `<tool>: <arg>` 当指令执行。

### Fixed

- `FunctionCallingAgent`:工具函数抛异常(含 `SkillError`)不再让整轮 `step()` 崩溃,而是归一成
  tool 消息喂回模型(只含稳定错误码 `tool.execution_failed` / CorespineError 的 code 与异常类型名;
  消息原文需 `include_error_message=True`);`fail_fast=True` 保留旧行为;审批错误与
  `KeyboardInterrupt` / `SystemExit` 不吞。
- 历史里坏 JSON 的 tool-call arguments 不再让 Anthropic / Gemini / Bedrock 适配器抛
  `JSONDecodeError`,统一回落为 `{"_raw": <原串>}`。
- `McpClientTool` 遇到缺 `result` 键的结果抛 `McpProtocolError`(code `mcp.invalid_result`)。
- `FunctionCallingAgent` / `ToolUsingAgent` / `DeepResearchAgent` 传入重名工具时构造即抛
  `ValueError`,不再静默覆盖。

- `FailoverProvider` / `StreamingFailoverProvider`:不可重试错(`NonRetryableProviderError`,
  4xx 坏请求)不再打遍整个池子并冷却全部下游,而是直接抛给调用方;游标 / 冷却表加锁。
- 各适配器按 vendor 状态码设 `retryable`:400 / 413 / 422 → `NonRetryableProviderError`;
  其余(网络 / 超时 / 408 / 429 / 5xx / 鉴权类)→ `ProviderError(retryable=True)`。
- `MiddlewareAgent` 步序取号加锁,多线程共享时不重号。
- usage 不再丢失:`FunctionCallingAgent` 多轮累加;`ChainAgent` 累加各段 usage 并拼接 artifacts;
  `AgentTool` 经 `ToolResult` 透传子 agent 的 usage / artifacts,`ToolUsingAgent` 汇总。

- trace 不再记入模型 / 对端可控的自由文本:`FunctionCallingAgent` 的 `tool_step.tool` 只取本地注册表
  里的工具名,未知工具记 `<unknown>`;`A2AAgentAdapter` 的 provenance 与 trace 用本地登记名
  (构造参数 `name=` 或构造期对 `remote.name` 的快照),不再每步读取对端自报名。

- `Coordinator.run_parallel` 可设 `timeout`(整批)/ `task_timeout`(单任务):挂死的 agent 不再卡住
  整批,超时的任务以 `error.code = "orchestration.timeout"` 的结果返回(缺省不限,与旧行为一致)。

### Added

- 审批记账 / 预检:`ToolCallLedger` / `InMemoryToolCallLedger` / `RecordedCall` / `begin_tool_call`、
  `preflight_tool_approvals`、`ApprovalMiddleware(ledger=)`、`FunctionCallingAgent(approve_before_execute=,
  on_approval=)`。
- 审批:`approval_scope` / `current_approval_scope`、`ConsumableApprovalGate`(`consume`)、`ApprovalConfigError`、
  `UnknownApprovalRequest`、`default_redactor` / `Redactor`、`ApprovalRequest.scope` / `.preview`、
  `ResumeTicket.scope`、`ManualApprovalGate(max_requests=, request_ttl=, now_fn=)` 与
  `resolve(uses=, ttl_seconds=)`、`ApprovalMiddleware(scope=, redact=)`、`require_approval(scope=, redact=)`、
  `enforce_tool_approval(target=, available=)`、`StepContext.inner_agent`、`reachable_tool_names` 与各 agent 的
  `tool_inventory()`;conformance `approval_is_consumed_once` / `approval_is_scope_bound`,
  `ToolExecutionHarness.run(scope=...)`。
- `ApprovalGateError`(code `approval.gate_error`)、`enforce_tool_approval`、`require_approval`、
  `make_approval_request(..., bind_values=True)`、`StepContext.cleanups`。
- `spineagent.llm.errors.NonRetryableProviderError` / `provider_error_from`;
  `spineagent.agent.agent.merge_usage`;`ToolResult.usage` / `ToolResult.artifacts`(可选字段)。
- `Coordinator.run_parallel(timeout=..., task_timeout=..., clock=...)`、`AgentTimeoutError`。
- `A2AAgentAdapter(name=...)`;conformance `TOOL_TRACE_INVARIANTS`(未知工具名不进 trace);
  `ToolExecutionHarness.run(trace=...)`。
- `spineagent.agent.trust`:`TaskText` / `untrusted` / `compose` / `lines_with_trust`;
  `SyntaxToolPolicy(parse_untrusted=...)`;`POLICY_INVARIANTS` 新增
  `untrusted_data_is_never_an_instruction`。
- `FunctionCallingAgent(fail_fast=..., include_error_message=...)`、`McpProtocolError`、
  `spineagent.tools.tool.index_tools_by_name`。
- `InProcessSandbox(clock=...)`:可注入时钟(默认 `time.monotonic`)。
- conformance:`SANDBOX_INVARIANTS` 新增 `timeout_takes_effect`;`APPROVAL_ENFORCEMENT_INVARIANTS` /
  `TOOL_TRACE_INVARIANTS` 从顶层 `spineagent` 导出。
- conformance:`APPROVAL_ENFORCEMENT_INVARIANTS` + `ToolExecutionHarness` 协议 +
  `ScriptedToolCallProvider`(离线脚本化 tool_calls 的 provider)。

### Documentation

- `docs/llms/`(随 wheel 分发)补齐 sandbox / middleware / artifact / approval / deep research / 信任边界
  与本次行为变化;`api.md` 不再写死过期版本号;README 模块表与 conformance 行补全。
- README / `llms.txt` / `docs/llms/` / `CLAUDE.md` 如实标注 `[mcp]` / `[a2a]` / `[sandbox]` 对应的真实后端、
  `tool_policies["llm"]`、`sandboxes["subprocess"]` 为占位、尚未实现(必抛 `SeamError`)。
- `ci.yml` / `release.yml` / `deploy/README.md` 的 corespine 下限与 `pyproject.toml` 对齐(`>=0.2.0`)。

### Changed

- `ApprovalMiddleware` 不再在 `before_step` 依据 `ctx.tools` 抛错;审批 request id 不再依赖本步
  宣告的工具集,而由工具名 + 参数内容派生。`mw_approval` trace 改为每次受审批调用一条
  (`gated_count=1`)。
- `MiddlewareAgent.step` 无论成败都会逆序执行 `ctx.cleanups`(逐个执行、各自捕获;本步出错时收尾失败只作为
  `__notes__` 附注,本步成功时单个原样抛出、多个抛 `ExceptionGroup`);整步在上下文副本里运行,步内设置的
  contextvar 不再泄漏给调用方。
- `Coordinator.run_parallel` 把调用方的 `contextvars` 上下文复制进每个工作线程。
- `DeepResearchAgent` 遇到审批挂起 / 拒绝时原样上抛,而不是当作一条失败的检索发现继续综合。

### Breaking

- 依赖「`ApprovalMiddleware` 在内层 agent 运行前、按 `ctx.tools` 抛 `ApprovalRejected` /
  `ApprovalPending`」的调用方:现在只有在受审批工具**真正被调用**时才抛;内层 agent 不调用该工具
  就不会抛。ManualApprovalGate 上旧的按「工具集」派生的 request id 不再出现,待审请求改为按调用派生。
- **审批批准改为一次性消费**:此前一次 resolve 后同工具同参数的调用都放行;现在缺省只放行一次,要旧行为传
  `resolve(..., uses=None)`。**resume 须回到同一作用域**:未设作用域时每次 step 是新作用域,resolve 后原样重跑会
  得到新的待审请求;用 `with approval_scope(exc.context["scope"]):` / `ResumeTicket.scope`,或给
  `ApprovalMiddleware(scope=会话 id)`。`ManualApprovalGate.resolve` 对未登记 / 已过期的 id 抛
  `UnknownApprovalRequest`(此前可预先批准)。`gated_tools` 含通配符、对应不到已知工具、与已注册工具仅大小写 /
  分隔符不同时抛 `ApprovalConfigError`(此前静默放行)。`ApprovalRequest` 新增 `scope` / `preview` 字段,
  请求 id 在设了作用域时随作用域变化;`ToolExecutionHarness.run` 新增 `scope` 关键字参数。
- **`FunctionCallingAgent` 缺省「先审后行」**:同一轮 tool_calls 里有任何受审批调用未获批时,该轮其它(未受审批的)
  工具也不执行(此前排在前面的会先执行);要旧行为传 `approve_before_execute=False`。在 `ApprovalMiddleware` 的步里
  因审批挂起后在同一作用域重跑,已成功执行过的调用复用记录结果而不再执行(此前会重放)。
- `InProcessSandbox.run(timeout=...)` / `Limits.timeout_seconds` 从「只记录」变为强制:求值超过
  timeout(缺省 `DEFAULT_LIMITS` 为 5 秒)判失败。
- `InProcessSandbox` 新增拒绝规则:`round(x, n)` 要求 `|n|` ≤ 2467(`limit_exceeded`);`int(s)` 要求
  数字串 ≤ 4300 字符(`limit_exceeded`,此前由宿主的 `int_max_str_digits` 判 `error`);`sum` 的 `start`
  只接受数值(`sum(lists, [])` 判 `disallowed`);值的容器嵌套深度 ≤ 100。`max_ops` / `ResourceUsage.ops`
  的单位从「AST 节点数」改为「工作量单位」(节点 + 按规模折算的大操作),同一表达式的 `ops` 会变大;
  `max_ops=None` 不再是无上限(硬上限 100 万单位)。
- `CalcTool` 拒绝超过上述上限的表达式(此前会长时间计算或抛 `RecursionError`)。
- `FunctionCallingAgent` 缺省不再让工具异常冒泡(需要旧行为传 `fail_fast=True`)。
- `McpClientTool` 缺结果键时抛 `McpProtocolError` 而非 `KeyError`。
- 重名工具从「后者静默覆盖前者」变为构造期 `ValueError`。
- 适配器抛出的 `ProviderError` 现在带 `retryable=True`(此前为类默认 False);坏请求改抛子类
  `NonRetryableProviderError`(仍是 `ProviderError`),`FailoverProvider` 对它不回退。
- `FunctionCallingAgent` 的 `usage` 由「末轮」改为「各轮累加」。
- `SyntaxToolPolicy` 缺省不再解析被标为数据的文本:依赖「pipeline 上游输出驱动下游执行工具」的
  调用方需显式 `SyntaxToolPolicy(parse_untrusted=True)`;`SummaryMiddleware` 的摘要也属数据。
