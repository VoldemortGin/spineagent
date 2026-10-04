# ADR 0003 — 指令与数据分通道:上游输出不是给下游的指令

- 状态:已接受
- 日期:2026-10-04
- 相关:`agent/trust.py`、`agent/policy.py`、`orchestration/coordinator.py`、`agent/middleware.py`、
  `agent/tool_using.py`、`agent/as_tool.py`、`protocol/a2a/seam.py`、`protocol/mcp/seam.py`

## 问题

`SyntaxToolPolicy` 把**整个** task 按 `<tool>: <arg>` 逐行解析;`Coordinator.run_pipeline` 把上游
输出原样当下游 task;`AttachmentMiddleware` 把附件正文拼进 task;`ToolUsingAgent` 的 `$prev` 把
上一步工具结果拼进下一次工具调用的参数(经 `AgentTool` 就成了子 agent 的 task)。离线探针复现:
A2A 对端回复里带一行 `nuke: now`,pipeline 下游的 `ToolUsingAgent` 真的执行了 `nuke`。任何能影响
上游输出的一方(远端 agent、被检索的文档、附件作者、工具返回)都能借此驱动下游执行工具。

## 决策

1. **两个通道,在类型层面区分。** 新增 `TaskText(str)`:一个携带「不可信字符区间」的 str 子类。
   - `untrusted(text)`:整段标为数据;`compose(*parts)`:拼接并保留各段区间(全可信时退回 plain
     str);`lines_with_trust(text)`:按行给出「是否完全可信」。
   - **plain `str` = 调用方直接给的指令**(向后兼容:`agent.step("calc: 1+1")` 行为不变)。
   - 因为是 str 子类,对只认 str 的代码(LLM prompt、trace 长度、序列化、相等比较)完全透明,
     不需要改 `Agent.step(task: str)` 协议。
2. **数据在源头打标。** 以下一律标为数据:
   - `Coordinator.run_pipeline` 传给下游的上游输出(`ChainAgent` 复用它,嵌套 pipeline 自然覆盖);
   - `AttachmentMiddleware` 前置的附件内容;`SummaryMiddleware` 生成的摘要(模型输出);
   - `ToolUsingAgent` 经 `$prev` 拼进参数的上一步工具结果;
   - `AgentTool` 返回的子 agent 产出、`McpClientTool` 返回的 MCP 结果、`A2AAgentAdapter` 返回的
     对端应答。
3. **指令解析只看可信行。** `SyntaxToolPolicy` 只把**完全由可信字符组成**的行当作可能的指令;
   含任何不可信字符的行(包括「可信前缀 + 数据后缀」的拼接行)一律按正文处理——数据仍然出现在
   最终答案 / 转述里,只是不被执行。
4. **旧行为需显式打开。** `SyntaxToolPolicy(parse_untrusted=True)` 恢复「整段都解析」。这是唯一的
   开关,放在唯一的指令解析器上;只有确认上游可信的调用方才该打开。
5. **conformance 钉死。** `POLICY_INVARIANTS` 新增 `untrusted_data_is_never_an_instruction`:任何
   ToolPolicy 对被标为数据的文本(整段或拼接段)都必须返回 `Finish`。打开旧行为的 policy 会被这格
   如实标红(有测试证明)。

## 信任边界(写给使用者)

- 可信 = 你的代码直接传给 `agent.step()` 的字符串字面量 / 你自己拼的 plain str。
- 不可信 = 一切来自模型、工具、远端 agent、MCP server、附件、上游 agent 的文本。
- 自己转手这些文本时,用 `untrusted()` 包住源头、用 `compose()` 拼接;对 `TaskText` 做普通 str
  运算(`+`、f-string、`strip`、切片)得到的是 plain str,**标记会丢、会被重新当成指令**。
- 本 ADR 只约束**指令语法解析**(离线 `SyntaxToolPolicy`)。真 LLM function-calling 下模型本身会
  读到数据里的「指令」——那是提示注入问题,由审批闸(ADR 0002)与工具最小授权兜底,不在本 ADR
  能力范围内。

## 后果

- 缺省安全:pipeline / 嵌套 pipeline / 附件 / 工具结果回灌 / A2A / MCP 返回里的指令语法不再执行。
- 行为变化:依赖「上游输出驱动下游执行工具」的 pipeline 需要显式 `parse_untrusted=True`;
  `SummaryMiddleware` 产出的摘要不再可被解析为指令。见 CHANGELOG。
