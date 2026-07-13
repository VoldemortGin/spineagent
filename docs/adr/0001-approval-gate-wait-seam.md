# ADR 0001 — 审批门 / Wait 缝（框架侧）

- 状态:已接受(批次 3.1a)
- 日期:2026-07-14
- 相关:家族 ADR 0001(边界 / 依赖方向)、corespine trigger / credential 缝

## 背景

对标 n8n 的 Wait 节点与 Send-and-Wait 审批**概念**(n8n 是 fair-code 许可,只学概念、绝不抄
代码):agent 执行到风险动作前应能暂停,等人类批准 / 拒绝后再恢复。本批只做**框架机制**;产品侧
的审批收件箱 UI 留给 spinestudio 后续批。

## 决策

1. **新增 ApprovalGate 缝**(`agent/approval.py`),照家族缝元模式:Protocol + 离线确定性默认 +
   `make_*` / Registry 工厂 + 参数化 conformance。
   - `ApprovalRequest` 只携带**定位摘要**:审批类别 code、确定性 request id、工具名、参数 schema
     指纹(sha256 前 16 位,只覆盖键名 + 值类型)与计数——**绝不含参数正文 / 值**。
   - `review(request) -> Decision` 给出三态:approved / rejected / pending。
   - 两个离线默认:`AutoApprovalGate`(工具名 glob allow/deny 策略表,纯配置、永不 pending)与
     `ManualApprovalGate`(进程内挂起:review 登记待审、`resolve(request_id, decision)` 落决议并铸
     一枚一次性 resume token、`redeem` 消费)。

2. **一次性 resume token**:明文只在 resolve 返回一次,落存只存 sha256 哈希;`redeem` 校验 + 标记
   consumed,重放必抛 `InvalidResumeToken`。存储走可插拔 `ResumeTokenStore`(默认框架内存
   `InMemoryResumeTokenStore`),模式照 spinestudio `refresh_store`,但**不落 corespine**(rule of
   three 未满:此处仅一个消费者,spinestudio 的是独立 SQLite 实现)。

3. **审批 middleware** 插进现有 middleware 链(`ApprovalMiddleware`):步执行前若本步激活了受审批
   工具(`gated_tools` 配置),询问 gate——approved 放行、rejected 抛 `ApprovalRejected` 断路、
   pending 抛 `ApprovalPending` 挂起。默认 `gated_tools` 为空 ⇒ 零行为变化(opt-in)。

## 关键裁决

- **pending = 抛类型化错误而非同步阻塞 / 新增续体机制。** 同步阻塞冻住线程等人不可接受;给 agent
  循环加「挂起 / 恢复续体」是对 ToolUsingAgent / FunctionCallingAgent / MiddlewareAgent 的大改。
  家族既有的断路手段正是**抛类型化 `CorespineError` → 编排层经 `error_to_dict` 捕获进
  `AgentResult.error`**(见 AgentResult docstring 与 Coordinator resilient)。pending 复用这条缝:抛
  一个可重试、带 `request_id` 的 `ApprovalPending`,调用方 out-of-band `resolve` 后**重跑 step** 恢复。
  step 可安全重跑正因决议幂等(见下),**零新增** agent 循环机制,纯组合既有模式。

- **决议幂等 = request id 的稳定函数。** middleware 构造的 request id **刻意排除步序计数器**,只由
  (code, 工具集, schema 指纹) 派生,故挂起的 run 在 resolve 后重跑 step(步序自增)仍稳定命中同一
  request id 与已落决议。`review` 对同一 request 恒返回同一决议;`resolve` 首落者胜,相同决议重复
  幂等、冲突决议抛 `ApprovalConflict`。代价:同一 (code, 工具集) 的决议被记住并复用;要按次审批就换
  code / nonce / 换 gate。

## conformance(钉死)

新增 `APPROVAL_INVARIANTS`(auto / manual × 4 条):review 返回 Decision 三态、gate 有名、review
幂等、请求摘要不含参数正文(负向)。专测(`tests/test_approval.py`)另钉:resume token 一次性(重放
必败)、决议幂等 / 冲突、审批 trace 只记 code/计数/决议无正文、默认配置零行为变化(回归对照)。

## corespine 缺口

**无。** corespine 0.4.0 现有面已够用:`CorespineError`(类型化边界错误 + error_to_dict 归一)、
`Registry`(缝注册表)、`InProcessPrivacyTraceSink` / `FORBIDDEN_KEYS`(隐私 trace)。一次性 token
存储在本包内薄实现,未触发上提 corespine 的证据门槛。corespine 保持只读、未改一行。

## 后果

- 家族获得「风险动作前停下等人」的框架机制,离线确定性、零网络、隐私安全。
- 产品侧(spinestudio)可在后续批组合:`ManualApprovalGate.pending()` 喂审批收件箱、resolve 走
  管理台、resume token 授权前端恢复。
- 测试 352 → 382(+30 零回归);mypy / ruff / import-clean 全绿。
