# ADR 0002 — 审批是工具调用点上的强制闸(取代 ADR 0001 决策 3 的「步前声明检查」)

- 状态:已接受(2026-10-04 修订:批准语义改为一次性消费 + 作用域,见「修订记录」)
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
     `contextvars.ContextVar`,并在 `StepContext.cleanups` 登记弹出回调;`MiddlewareAgent` 无论成败
     逆序**逐个**执行 cleanups(各自捕获,一个失败不跳过其余的),并且整步在调用方上下文的**副本**里跑
     ——作用域结构上严格限定在被包裹的那一步(修订:审查复现自定义 cleanup 抛异常时弹出被跳过,同一线程
     之后无审批配置的请求被拦下、trace 串到上一个请求的 sink;长驻线程池会被永久污染)。contextvar 天然覆盖同一上下文里的
     嵌套 agent、闭包里的 agent、`AgentTool`;`Coordinator.run_parallel` 为每个分支复制调用方上下文
     (`contextvars.copy_context()`),故并行编排与 `DeepResearchAgent` 的并行检索里闸同样生效。
   - 静态绑定:`require_approval(tool, gate)` 把闸包进工具本身(`FunctionTool` 换包装函数、单串参
     `Tool` 换包装类)。它不依赖任何上下文——裸线程、第三方 agent 执行它也绕不过去。
   评估过「只做静态包装」:`ApprovalMiddleware` 挂在外层,拿不到不透明内层 agent(如闭包里的
   agent)的工具对象,无法替它包装;而「中间件改写内层 agent 的工具表」要么侵入各 agent 私有字段、
   要么对不透明 agent 再次 fail-open。故以动态作用域承接现有中间件 API,以静态包装补齐跨线程 /
   第三方执行点。
3. **request id 由内容与作用域派生,不依赖步序。** id = sha256(code, 工具名, schema 指纹,
   sha256(规范化参数值), 作用域)前 16 位;规范化 = 键排序紧凑 JSON(非 JSON 值退回 repr——不稳定只会
   导致重新审批)。`make_approval_request(..., bind_values=True, scope=...)` 暴露同一派生;缺省
   `bind_values=False` / `scope=""` 保留旧的「只看 schema」语义,给显式调用方。
4. **批准是安全边界:缺省一次性消费、绑定作用域、只能批准已存在的请求。**(本条于修订时取代原
   「决议幂等」。原条款下审查复现:只 resolve 一次、从不 redeem,同一步重复 6 次、换一个 agent 再
   6 次,共执行了 12 次;还能离线算出 id 预先批准。)
   - **消费**:`review` 仍是纯查询(幂等,不变量不变);可核销的门(`ConsumableApprovalGate`,如
     `ManualApprovalGate`)额外实现 `consume(request)`。执行闸在 review 得到 APPROVED 后原子核销一次,
     核销不到(额度已用尽 / 过期)即重新登记为待审、本次不执行。`resolve(..., uses=1)` 缺省只放行
     **一次**匹配调用;`uses=N` 放行 N 次;`uses=None` 是显式可选的旧「决议幂等」模式(有效期内同一
     作用域里同参调用无限次放行——风险:一次批准可被任意多次重放,只应给确实需要的批处理场景)。
     不实现 `consume` 的门(`AutoApprovalGate` 策略表)的批准是常驻的策略放行。
   - **作用域(第三轮修订:必须显式)**:request 绑定到调用方的作用域(会话 / 用户的不透明字符串),作用域折进
     id,A 的批准对 B 无效。来源:`ApprovalMiddleware(scope=)` / `require_approval(scope=)` > 外层
     `approval_scope(...)`;`ApprovalMiddleware` 把它设为外层作用域传给嵌套 agent / `require_approval` 包装(嵌套与
     叠加共享同一请求)。**库不生成隐式作用域**:第二轮的「缺省每次 step 新建一个」让「挂起 → 批准 → 原样重跑」
     三次得到三个 id、永远执行不了,收件箱里堆着批不掉的请求;「`require_approval` 按包装实例生成」让模块级共享的
     工具对所有用户是同一个作用域。现在:
     - **会产生待审请求的门**(可核销的 `ConsumableApprovalGate`,如 `ManualApprovalGate`)没有显式作用域时抛
       `ApprovalConfigError`——`ApprovalMiddleware` 在 `before_step`(内层 agent 运行之前),`require_approval` 在
       调用时(执行之前;构造时还不知道调用方会不会在外层设作用域);不实现 `consume` 的第三方门若在没有作用域时
       返回 PENDING,同样抛 `ApprovalConfigError`。信息原文:「这个审批门会产生待审请求(需要人工决议 / 可核销),
       必须显式提供作用域:请传入 scope=<会话或用户的唯一标识>……」(含原因与唯一性要求)。
     - **同步门**(`AutoApprovalGate` 等立即给出决定、永不挂起的门)不需要作用域,行为不变。
     - **唯一性由调用方保证**:作用域是不透明字符串,库无法保证唯一——它必须在共享同一个门的所有调用方之间唯一
       (建议 租户 id + 会话 id)。在这个前提下,「同作用域 + 同工具 + 同参数」就是同一个请求、共用同一个批准,这是
       定义内行为(审批人批准的正是这份内容);不同作用域互相隔离(conformance `approval_is_scope_bound`)。
     - **批准绑定到登记记录**:`ManualApprovalGate.consume` 除 id 匹配外,还要求请求与登记时的请求逐字段相等(code /
       工具 / 参数指纹与计数 / 作用域 / 完整规范化参数),手工拼一个同 id 的请求核销不了别人的批准。
     - **`feed_back` 模式**:喂回模型的文本只有 code 与 request id(不含作用域);调用方从
       `AgentResult.held_approvals`(每项 `code` / `request_id` / `scope` / `tool`)拿到待审请求,据它 resolve。
   - **请求生命周期(第三轮修订:有界且 fail-closed)**:`ManualApprovalGate.resolve` 只能决议**已登记、未过期**的
     待审请求,未知 id 抛 `UnknownApprovalRequest`(不得预先批准)。请求表有存活期(`request_ttl`;批准 / 拒绝的
     有效期缺省同此,`resolve(ttl_seconds=)` 可单独指定)与两级上限:每个作用域最多 `max_pending_per_scope`(缺省 64)
     条待审、全表最多 `max_requests`(缺省 1024)条(含已批准未核销)。到上限时**拒绝新请求**(`review` 抛
     `ApprovalGateError`,执行闸据此不执行),绝不淘汰别人的待审或已批准条目——第二轮「满了淘汰最早登记的」让一个
     模型回合的 1100 个调用挤掉了另一个作用域里的合法待审请求。另外 `FunctionCallingAgent(max_tool_calls_per_turn=64)`
     限制一轮 tool_calls 的数量:超出时整轮不执行、不送审,喂回模型「调用数超过上限」。拒绝不被消耗。核销发生在
     **执行之前**:工具随后抛异常,这次批准也已用掉(要重试须重新批准)。
   - **审批人看得到要批准的完整内容(第三轮修订)**:request id 哈希的是完整规范化参数(批准 X 只放行字节完全相同的
     X),所以审批人也必须能看到完整的 X。第二轮只给了截断 + 按键名子串打码的 preview:200 字符之后才不同的两次调用
     preview 逐字节相同;`author` / `passage` / `max_tokens` / `session_name` 都被打成 `***`;嵌套 dict 的键是模型
     自己定的,起名 `to_token` 就能把收款账户藏起来;API 里取不到完整参数。现在:
     - `ApprovalRequest.canonical_arguments`(键排序紧凑 JSON,正是 id 所哈希的内容)与 `ApprovalRequest.arguments()`
       (解析后的新 dict)给审批 UI / 审批人。**审批人应基于完整参数而不是 preview 做决定。**
     - `preview` 只作列表展示:每个值 / 键只截断一次(值 200、键 64 字符),省略提示如实写「省略 N 字符」并附完整值的
       sha256 前缀,尾部不同的两个请求在 preview 层面也可区分。
     - **缺省不打码**模型提供的参数。打码是调用方的显式声明:`ApprovalMiddleware(sensitive_args={工具名: [参数路径]})`
       / `require_approval(sensitive_args=[参数路径])`,路径用点号穿过 dict(`"password"`、`"body.to_token"`);被打码的值
       显示为 `***(sha256:<前缀>)`,不同的值仍可区分。打码只作用于 preview,完整参数不受影响。按键名猜测的缺省规则
       与可注入的 `Redactor` 钩子一并移除。
     - **这与 trace 隐私不冲突**:trace 隐私宪章约束的是可观测性通道(运维 / 观测管道的旁路,可能被批量导出、长期留存、
       给无授权的人看),所以 trace 只记 code / 计数 / 决议;审批接口是有授权的人决定「放不放行这次具体动作」的通道,
       看不到参数的审批没有意义。完整参数与 preview 不进 trace、不进 `repr`(也就不进以 repr 打日志的地方),只出现在
       gate 收到的请求与 `pending()` 里;核销时要求请求与登记时逐字段相等(含完整参数)。
   - **ticket 只是 resume 句柄**:`redeem(token)` 拿回 `ResumeTicket(request_id, decision)`;它不参与执行闸判定。
     放行额度在执行点核销,所以持有 / 重放 ticket 都不能让同一批准多执行一次;token 本身仍一次性(ADR 0001 不变)。
     作用域由调用方显式提供,ticket 不再携带(第二轮那张「request id -> 作用域」的旁表先进先出淘汰后作用域变成空串,
     已删除)。`InMemoryResumeTokenStore` 有界(`max_tokens`,超出时最早签发的未兑现 token 失效——它只是句柄)、
     过期(`ttl`)、加锁,兑现即删除。
   - **叠加**:同一调用被同一个门在一次执行闸里只审 / 核销一次(嵌套 `ApprovalMiddleware` 共享作用域;
     执行点把被执行的工具对象传给 `enforce_tool_approval(target=)`,工具自带 `require_approval` 闸时
     同一个门交给工具自己审)。
4a. **受审批工具名的校验。** 名字必须是确切工具名:构造时拒绝空名与通配符(`*?[]`——不支持 glob,写了就报错)。
   首次使用时若能推断被包裹 agent 的工具清单(`reachable_tool_names`:`FunctionCallingAgent` / `ToolUsingAgent` /
   `MiddlewareAgent` / `ChainAgent` / `AgentTool` / `DeepResearchAgent` 实现 `tool_inventory()`):
   - 与清单里某个工具**只差大小写 / 分隔符**的名字:抛 `ApprovalConfigError`(这几乎一定是笔误);
   - **在清单里找不到**的名字:发一次 `UserWarning`(每个 `ApprovalMiddleware` 实例一次;消息只含配置里的工具名),
     不报错——第二轮报错误伤了三种合法配置且没有逃生口:`FunctionTool` 里套着带受审批工具的 agent(清单看不到
     工具函数内部)、全站共用一份受审批名单、同一配置用于缺少该工具的 agent 变体。需要严格校验的调用方传
     `ApprovalMiddleware(strict_names=True)`,找不到即报 `ApprovalConfigError`。
   推断不了清单(闭包式 `FunctionAgent` 等不透明 agent)时,执行点检测「受审批名与已注册工具仅大小写 / 分隔符
   不同」并 fail-closed。**名字校验只防笔误,不是安全边界**:按名字 gate 对「同一函数以别名注册」不设防,也挡不住
   调用方自建线程(见「已知边界」);安全场景用 `require_approval`(按工具对象绑定)。
5. **未批准 = 不执行。** 缺省 rejected 抛 `ApprovalRejected`、pending 抛 `ApprovalPending`(带
   `request_id` / `scope`)。异常从执行点一路冒到调用方;`AgentTool` 嵌套照常上抛;`DeepResearchAgent`
   不让弹性并行把审批错误吞成一条失败发现,而是在收集后原样重抛。`FunctionCallingAgent(on_approval=
   "feed_back")` 是调用方显式选择的**不中断模式**:本执行点的挂起 / 拒绝作为 tool 结果(只含 code 与
   request id)喂回模型,整步不抛、不重跑;审批人照样在 `pending()` 里看到请求。
5a. **挂起后的重跑语义:同一轮内不重放,跨轮至少一次(修订)。** 恢复靠「批准后在同一作用域里重跑这个 run」。
   审查复现:一步里 `send_email` 在前、受审批工具在后,pending 时重跑 3 次、resolve 后再跑 1 次,`send_email` 共执行
   4 次。第二轮曾为此加过「步内记账」(按作用域字符串记录已执行调用的结果、重跑时复用),复审证明它本身是漏洞:
   同一会话作用域里用户的**新任务**被当成重跑,副作用被静默吞掉而模型被告知 `ok`;两个租户的作用域字符串相同时
   一方的模型直接拿到另一方工具的返回值。决策:**整体移除步内记账,不存在任何跨 run 的工具结果复用**,语义改为:
   - **同一轮内不重放**(保留):`FunctionCallingAgent(approve_before_execute=True)`(缺省)在模型给出一轮
     tool_calls 后、执行任何一个之前预检整轮(只 review、不核销;待审的被一次性登记,审批人一次看到整批);有任何
     一个未获批,该轮**一个工具都不执行**。`approve_before_execute=False` 恢复逐个执行。
   - **跨轮至少一次(at-least-once)**:因审批挂起后**重跑**该 run,此前各轮已执行过的工具(含未受审批的、以及此前
     已获批并执行过的受审批调用——一次性批准已被核销,会重新挂起)会**再次执行**。调用方二选一:让工具幂等;
     或用不中断模式 `on_approval="feed_back"`——挂起 / 拒绝作为 tool 结果喂回模型,run 正常结束,没有重跑,也就
     没有重放;批准后由**下一个任务**去执行(见下文「推荐用法」)。`ApprovalPending` 的消息与 docstring 写明这一点。
   - 评估过「调用方持有的续跑上下文」(`ApprovalPending` 携带已完成的调用与消息历史,`run(resume=...)` 从挂起点
     继续,状态只在调用方手里):`Agent.step` 协议没有续跑通道,而挂起可以发生在嵌套 agent 里(`AgentTool` /
     `ChainAgent` / `run_parallel` / `DeepResearchAgent` / 工具函数里的子 agent),只在最内层 `FunctionCallingAgent`
     保存进度无法恢复外层的进度;要让所有组合 agent 都可续跑是协议级改动。代价大,本轮不做。
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
2. 原样重跑不绕过,且同一作用域里三次重跑命中同一 request id;
3. 获批后在同一作用域同参重跑恰好执行一次;改参数得到新 request id 并重新挂起;
4. 一次批准只放行一次:同一 run 里重复的同参调用与之后的重跑都重新挂起(`approval_is_consumed_once`);
5. A 作用域的批准对 B 无效(`approval_is_scope_bound`);
6. 审批门抛异常时执行次数为 0(`ApprovalGateError`);
7. 未受审批的工具不受审批门影响。

## 已知边界

- 动态作用域靠 contextvar:**动态作用域不跨越调用方自建的线程**。调用方(或工具函数内部)自行起线程 /
  线程池执行 agent 时,`ApprovalMiddleware` 的闸在那条线程里不存在——审查复现:工具内用 `ThreadPoolExecutor` 跑子
  agent,在 deny-all 下受审批工具仍执行了 1 次;复审换个形状(外层 agent 自己也注册了同名工具,名字校验因此看不出
  问题)同样绕过。**名字校验不能也不打算发现这种情形。** 这种场景只有 `require_approval`(静态绑在工具对象上,
  conformance 覆盖裸线程)可靠。若必须用动态作用域,在起线程的地方用 `bind_context` 把当前上下文带过去:
  `pool.submit(bind_context(inner.step), task)`(`spineagent.orchestration.coordinator.bind_context`,
  `contextvars.copy_context().run` 的薄封装)。
- 自定义 agent 若自己执行工具,须在调用前调 `enforce_tool_approval`,或只接受经
  `require_approval` 包装过的工具。
- request id 是参数值的哈希而非明文;对低熵参数(如 `yes` / `no`)可被字典猜测,request id 不是
  参数保密手段。(参数预览本来就给审批人看,见决策 4。)
- 按名字 gate 对别名注册不设防;推断不了工具清单的不透明 agent 只能检测「近似名」写错,完全写错
  的名字(如把 `delete_file` 写成 `rm`)在不透明 agent 下仍无法发现——用 `require_approval`。

## 后果

- `ApprovalMiddleware` 不再在步前依据 `ctx.tools` 抛错(那条路径本身就是漏洞来源)。修订后的破坏性
  变化:批准缺省一次性消费;resume 须回到同一作用域;`resolve` 只接受已登记的请求;受审批名含通配 /
  对应不到已知工具时报错;`ManualApprovalGate.pending()` 的条目带预览。新增 `ApprovalGateError`、
  `enforce_tool_approval`、`require_approval`、`make_approval_request(bind_values=)`、
  `StepContext.cleanups`。行为变化见 CHANGELOG。
- 原 `tests/test_approval.py` 里以 `FunctionAgent` 假执行的中间件测试全部改写为端到端真实路径。

## 修订记录

- 2026-10-04(审查第二轮):决策 4 由「决议幂等」改为「一次性消费 + 作用域 + 只能批准已存在的请求 +
  审批人可见的参数预览 + ticket 只是 resume 句柄」;新增决策 4a(受审批工具名校验)。原因:审查复现
  一次批准被同一步 / 跨 agent 重放 12 次、可离线预先批准、审批人看不到参数值、写错的工具名静默放行。
  本 ADR 尚未随版本发布,故就地修订而不另开编号(ADR 0002 取代 ADR 0001 决策 3 时用的是新编号,
  那是因为 0001 已发布)。
