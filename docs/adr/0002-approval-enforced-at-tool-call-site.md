# ADR 0002 — 审批是工具调用点上的强制闸(取代 ADR 0001 决策 3 的「步前声明检查」)

- 状态:已接受
- 日期:2026-10-04
- 相关:ADR 0001(审批门 / Wait 缝);`agent/approval.py`、`agent/middleware.py`、
  `agent/function_calling.py`、`agent/tool_using.py`、`orchestration/coordinator.py`

## 问题

ADR 0001 的 `ApprovalMiddleware` 在 `before_step` 里只看 `ctx.tools`(本步「声明的」可用工具名),
据此决定是否询问 gate;`MiddlewareAgent` 并不把 `ctx.tools` 传给内层 agent,内层 agent 执行工具时
也从不咨询 gate。离线探针复现了两种 fail-open:

1. 链里没有 `DynamicToolMiddleware` 播种 `ctx.tools` 时,闸看到空集直接放行,内层
   `FunctionCallingAgent` 照常执行受审批工具。
2. 用 `DynamicToolMiddleware({0: [...]})` 只在步序 0 宣告工具时,第一次被拒,原样重跑(步序变 1)
   宣告面变空,闸放行,工具执行。

原有测试没抓到,是因为内层用的是不执行任何工具的 `FunctionAgent`,断言的只是「中间件抛没抛错」,
而不是「真实工具执行了几次」。

## 为什么之前的设计会 fail-open

闸检查的是**声明**(`ctx.tools`),执行的是**另一处代码**(内层 agent 的工具循环),两者之间没有任何
绑定:声明可以缺失、可以随步序变化、可以被别的中间件改写,而执行点对此一无所知。凡是「检查与执行
分离、靠约定对齐」的闸,只要约定漏接一处就静默放行——这正是缺省安全的反面。request id 依赖
「本步激活的工具集」也让决议跟着声明面漂移。

## 决策

1. **闸下沉到真实执行点。** 全仓的工具执行点只有两处:`FunctionCallingAgent.step`(`tool.invoke`)与
   `ToolUsingAgent.step`(`tool.run`)。其余会「执行工具」的构件都经由它们:`AgentTool` 把子 agent
   当工具(子 agent 内部仍走这两处)、`ChainAgent` / `MiddlewareAgent` / `Coordinator` 只编排
   agent、`DeepResearchAgent` 的检索走 `FunctionCallingAgent`、`skill_as_function_tool` /
   `McpClientTool` 是被上述两处执行的工具。两处执行点在**每一次**真实调用前调
   `enforce_tool_approval(真实工具名, 参数)`;FunctionCallingAgent 用校验后的参数 dict,
   ToolUsingAgent 用 `{"arg": <替换 $prev 后的实参>}`。
2. **门到达执行点的两条通道。**
   - 动态作用域:`ApprovalMiddleware.before_step` 把 `(gate, gated_tools, code)` 压进一个
     `contextvars.ContextVar`,并在 `StepContext.cleanups` 登记弹出回调;`MiddlewareAgent` 在
     `finally` 里逆序执行 cleanups,作用域严格限定在被包裹的那一步。contextvar 天然覆盖同一上下文里的
     嵌套 agent、闭包里的 agent、`AgentTool`;`Coordinator.run_parallel` 为每个分支复制调用方上下文
     (`contextvars.copy_context()`),故并行编排与 `DeepResearchAgent` 的并行检索里闸同样生效。
   - 静态绑定:`require_approval(tool, gate)` 把闸包进工具本身(`FunctionTool` 换包装函数、单串参
     `Tool` 换包装类)。它不依赖任何上下文——裸线程、第三方 agent 执行它也绕不过去。
   评估过「只做静态包装」:`ApprovalMiddleware` 挂在外层,拿不到不透明内层 agent(如闭包里的
   agent)的工具对象,无法替它包装;而「中间件改写内层 agent 的工具表」要么侵入各 agent 私有字段、
   要么对不透明 agent 再次 fail-open。故以动态作用域承接现有中间件 API,以静态包装补齐跨线程 /
   第三方执行点。
3. **request id 由内容派生,不依赖步序。** id = sha256(code, 工具名, schema 指纹, sha256(规范化参数
   值))前 16 位;规范化 = 键排序紧凑 JSON(非 JSON 值退回 repr——不稳定只会导致重新审批)。
   `make_approval_request(..., bind_values=True)` 暴露同一派生;缺省 `bind_values=False` 保留旧的
   「只看 schema」语义,给显式调用方。
4. **批准的消费语义 = 沿用 ADR 0001 的「决议幂等」。** 决议绑定在内容派生的 request id 上并被记住:
   同一工具 + 同一参数的调用(包括 resolve 后原样重跑、或同一 run 内的重复调用)都命中已落决议;
   参数一变就是新请求、须重新审批。**批准不是一次性消费**——一次性的是 resume token(ADR 0001 不变)。
   理由:恢复靠「重跑整步」,一步内若有多个受审批调用,前面已批准的调用在重跑时必须仍能通过,否则
   永远走不到后面的调用。代价写明:重跑会重放该步内已执行过的工具副作用(ADR 0001 既有语义)。
5. **未批准 = 不执行,保持现有对外语义。** rejected 抛 `ApprovalRejected`、pending 抛
   `ApprovalPending`(带 `request_id`,与 `ManualApprovalGate` / `ResumeTicket` 的「resolve 后重跑」
   流程一致),不改成「拒绝结果喂回模型」。异常从执行点一路冒到调用方;`AgentTool` 嵌套照常上抛;
   `DeepResearchAgent` 不让弹性并行把审批错误吞成一条失败发现,而是在收集后原样重抛。
6. **fail-closed。** 作用域里有受审批工具时:gate.review 抛任何 `Exception`、或返回非 `Decision`,
   一律抛 `ApprovalGateError`(code `approval.gate_error`),工具不执行。未配置审批(无作用域、
   `gated_tools` 为空)时执行点只读一次 contextvar 即返回,行为与修复前完全一致。
   `ApprovalMiddleware.before_step` 若在 `MiddlewareAgent` 之外被手动调用而没跑 cleanups,作用域会
   留在当前上下文——只会多拦,不会少拦。

## 保证的不变量(`APPROVAL_ENFORCEMENT_INVARIANTS`)

参数化 10 个执行形态:`FunctionCallingAgent`、`ToolUsingAgent`、`DynamicToolMiddleware` 在审批前
(宣告)/ 在审批后(清空声明面)、嵌套 `MiddlewareAgent`、`AgentTool` 嵌套、`ChainAgent`、
`run_parallel`、`DeepResearchAgent`、`require_approval` + 裸线程。每格都用带副作用计数的真实工具
函数断言:

1. 未获批准(rejected / pending)的受审批工具执行次数为 0;
2. 原样重跑不绕过,且三次重跑命中同一 request id;
3. 获批后同参重跑恰好执行一次;改参数得到新 request id 并重新挂起;
4. 审批门抛异常时执行次数为 0(`ApprovalGateError`);
5. 未受审批的工具不受审批门影响。

## 已知边界

- 动态作用域靠 contextvar:调用方若自行起线程 / 线程池执行 agent 且不复制上下文,中间件作用域
  不会跟过去。需要跨任意线程的强保证时用 `require_approval` 绑在工具上(conformance 已覆盖)。
- 自定义 agent 若自己执行工具,须在调用前调 `enforce_tool_approval`,或只接受经
  `require_approval` 包装过的工具。
- request id 是参数值的哈希而非明文;对低熵参数(如 `yes` / `no`)可被字典猜测,request id 不是
  参数保密手段。

## 后果

- `ApprovalMiddleware` / `ManualApprovalGate` / `ResumeTicket` 的对外用法不变;`ApprovalMiddleware`
  不再在步前依据 `ctx.tools` 抛错(那条路径本身就是漏洞来源)。新增 `ApprovalGateError`、
  `enforce_tool_approval`、`require_approval`、`make_approval_request(bind_values=)`、
  `StepContext.cleanups`。行为变化见 CHANGELOG。
- 原 `tests/test_approval.py` 里以 `FunctionAgent` 假执行的中间件测试全部改写为端到端真实路径。
