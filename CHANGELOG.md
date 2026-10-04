# Changelog

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### Security

- 审批门改为工具调用点上的强制闸(ADR 0002):`FunctionCallingAgent` / `ToolUsingAgent` 在每一次
  真实调用工具前按「真实工具名 + 规范化参数」向审批门 review;未批准不执行。中间件组合顺序、嵌套
  agent、`AgentTool`、`ChainAgent`、`Coordinator.run_parallel`、`DeepResearchAgent` 下均生效;
  审批门故障时 fail-closed。

### Added

- `ApprovalGateError`(code `approval.gate_error`)、`enforce_tool_approval`、`require_approval`、
  `make_approval_request(..., bind_values=True)`、`StepContext.cleanups`。
- conformance:`APPROVAL_ENFORCEMENT_INVARIANTS` + `ToolExecutionHarness` 协议 +
  `ScriptedToolCallProvider`(离线脚本化 tool_calls 的 provider)。

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
