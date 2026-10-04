# Changelog

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### Security

- 审批门改为工具调用点上的强制闸(ADR 0002):`FunctionCallingAgent` / `ToolUsingAgent` 在每一次
  真实调用工具前按「真实工具名 + 规范化参数」向审批门 review;未批准不执行。中间件组合顺序、嵌套
  agent、`AgentTool`、`ChainAgent`、`Coordinator.run_parallel`、`DeepResearchAgent` 下均生效;
  审批门故障时 fail-closed。
- `InProcessSandbox`:消除热路径上被 beartype claw 每次调用重新装饰的嵌套函数(大 env 求值从秒级
  降到毫秒级);`timeout` 现在真正生效(按节点检查的协作式 deadline,超时判 `limit_exceeded`)。
- `CalcTool`:表达式长度 ≤ 4096、嵌套深度 ≤ 100,幂 / 乘法结果位数复用 sandbox 的昂贵二元运算
  守卫;越界立即抛 `ValueError`。
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
- `MiddlewareAgent.step` 无论成败都会逆序执行 `ctx.cleanups`。
- `Coordinator.run_parallel` 把调用方的 `contextvars` 上下文复制进每个工作线程。
- `DeepResearchAgent` 遇到审批挂起 / 拒绝时原样上抛,而不是当作一条失败的检索发现继续综合。

### Breaking

- 依赖「`ApprovalMiddleware` 在内层 agent 运行前、按 `ctx.tools` 抛 `ApprovalRejected` /
  `ApprovalPending`」的调用方:现在只有在受审批工具**真正被调用**时才抛;内层 agent 不调用该工具
  就不会抛。ManualApprovalGate 上旧的按「工具集」派生的 request id 不再出现,待审请求改为按调用派生。
- `InProcessSandbox.run(timeout=...)` / `Limits.timeout_seconds` 从「只记录」变为强制:求值超过
  timeout(缺省 `DEFAULT_LIMITS` 为 5 秒)判失败。
- `CalcTool` 拒绝超过上述上限的表达式(此前会长时间计算或抛 `RecursionError`)。
- `FunctionCallingAgent` 缺省不再让工具异常冒泡(需要旧行为传 `fail_fast=True`)。
- `McpClientTool` 缺结果键时抛 `McpProtocolError` 而非 `KeyError`。
- 重名工具从「后者静默覆盖前者」变为构造期 `ValueError`。
- 适配器抛出的 `ProviderError` 现在带 `retryable=True`(此前为类默认 False);坏请求改抛子类
  `NonRetryableProviderError`(仍是 `ProviderError`),`FailoverProvider` 对它不回退。
- `FunctionCallingAgent` 的 `usage` 由「末轮」改为「各轮累加」。
- `SyntaxToolPolicy` 缺省不再解析被标为数据的文本:依赖「pipeline 上游输出驱动下游执行工具」的
  调用方需显式 `SyntaxToolPolicy(parse_untrusted=True)`;`SummaryMiddleware` 的摘要也属数据。
