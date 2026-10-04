# Changelog

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### Security

- 审批门改为工具调用点上的强制闸(ADR 0002):`FunctionCallingAgent` / `ToolUsingAgent` 在每一次
  真实调用工具前按「真实工具名 + 规范化参数 + 作用域」向审批门 review;未批准不执行。中间件组合顺序、嵌套
  agent、`AgentTool`、`ChainAgent`、`Coordinator.run_parallel`、`DeepResearchAgent` 下均生效;
  审批门故障时 fail-closed。`require_approval(tool, gate)` 把闸绑进工具对象本身(不依赖上下文,裸线程 / 别名注册
  都绕不过)。
- 审批的批准是安全边界(ADR 0002 决策 4):
  - 批准**缺省一次性消费**,在执行之前核销(`resolve(uses=N)` 放行 N 次,`uses=None` 为显式可选的旧幂等模式);
    核销要求请求与登记时逐字段相等(含完整参数)。
  - 请求绑定调用方**显式提供**的作用域(`ApprovalMiddleware(scope=)` / `require_approval(scope=)` / 外层
    `approval_scope(...)`),A 的批准对 B 无效;会产生待审请求的门(`ManualApprovalGate` 等可核销的门)没有作用域时
    在任何工具执行前抛 `ApprovalConfigError`,库不生成隐式作用域。作用域须在共享同一个门的调用方之间唯一。
  - `ManualApprovalGate.resolve` 只接受已登记、未过期的请求(不能离线算出 id 预先批准)。请求表有界且 fail-closed:
    每个作用域最多 64 条「待审 + 未核销的已批准」、全表(缺省 16384,= 64 × 256 个满配额会话,构造参数
    `max_requests`,与每作用域上限解耦)满了拒绝新请求(`ApprovalGateError`),不淘汰既有条目;已决议的记录不占这份配额
    (批准核销完即删除,被拒绝的进另一张有界、较短 TTL 的去重表 `max_decided` / `decided_ttl`)——少数会话或一个被反复
    拒绝的作用域占不满全表、拒绝其它用户;
    `FunctionCallingAgent` 一轮最多 64 个工具调用,超出整轮不执行、不送审。
  - 审批人看得到要批准的完整内容:`ApprovalRequest.canonical_arguments` / `arguments()` 是 request id 所哈希的完整
    规范化参数;`preview` 只作列表展示(截断一次,如实注明省略字符数与摘要)。缺省不打码,打码由
    `sensitive_args` 显式声明(打码后仍可区分)。二者都不进 trace / repr。
  - 受审批工具名含通配符 / 空名、或与已注册工具仅大小写 / 分隔符不同时 fail-closed(`ApprovalConfigError`)。
- 同一轮内不重放副作用:`FunctionCallingAgent` 缺省**先审后行**——一轮 tool_calls 里有任何受审批调用未获批则整轮
  不执行。挂起后重跑 run 是**至少一次**语义(此前已执行过的工具会再次执行,库不复用任何跨 run 的结果);不想重跑用
  不中断模式 `on_approval="feed_back"`。
- `raise` 模式下多轮多审批会指数重跑(N 个受审批调用分布在 N 轮:批 2^N−1 次、首个动作执行 2^(N−1) 次;
  `run_parallel` 非 resilient 同理)。缺省仍为 `raise`(评估后不改:现有契约 / conformance 断言「未批准抛错」,改缺省会
  让不读 `held_approvals` 的调用方把「没执行」当成功),但:`ManualApprovalGate` 记录每个 request id 已核销的次数(有界
  `max_decided` / 短 TTL `decided_ttl`),重新登记的待审请求带 `ApprovalRequest.prior_executions` 并在 `preview` 首行注明,
  `ApprovalPending` 的消息与 `context["prior_executions"]` 提示「多个受审批调用的流程请使用 `on_approval="feed_back"`」。
  这类流程**必须**用 `feed_back`(每个动作恰好执行一次);`run_parallel` 审批场景用 `feed_back` 或 `resilient=True`。
- `InProcessSandbox`:消除热路径上被 beartype claw 每次调用重新装饰的嵌套函数(大 env 求值从秒级
  降到毫秒级);`timeout` 作为协作式 deadline 在节点之间检查(超时判 `limit_exceeded`)。注:这一条
  **不能**中断单个内建调用,单次求值的代价上界见下两条。
- `InProcessSandbox` 的耗时上界由**先验规模守卫**保证(审查复现 `round(1, -10**7)` 在一个节点内部跑了
  10 秒且返回成功):`round` 的 `|ndigits|` ≤ 2467、`int()` 的数字串 ≤ 4300 字符、`sum` 只做数值累加、
  值的嵌套深度 ≤ 100;每个节点**之后**也检查 deadline;新增总工作量预算(`max_ops` 计工作量单位,单节点内
  的大操作按输入 / 结果规模折算、文本按存储字节折算,`max_ops=None` 时仍有 100 万单位硬上限),反复引用同一个
  大值的写法被提前截住。深嵌套 env 不再让 `run()` 抛 `RecursionError`。
- `InProcessSandbox` 的内存上界由**按估算字节计的内存预算**保证(复审复现 14 KB 代码造出约 370 MB):每个节点产生
  的值在产生时按估算字节(宽字符串按实际宽度、容器递归)累加并检查,容器字面量 / 调用实参在超限处立即停下;
  `Limits.max_memory_bytes` 缺省 32 MiB,`None` 时仍有 128 MiB 硬上限。`MemoryError` / `RecursionError`(含解析器栈
  溢出)一律容住为 `limit_exceeded`,不再冒泡出 `run()`。
- `CalcTool`:表达式长度 ≤ 4096、嵌套深度 ≤ 100,幂 / 乘法结果位数复用 sandbox 的昂贵二元运算
  守卫;越界立即抛 `ValueError`。
- `MiddlewareAgent`:收尾回调逐个执行、各自捕获(一个失败不再跳过其余的),整步在调用方上下文的副本里跑——
  自定义中间件的 cleanup 抛异常不再让审批作用域 / trace 落点泄漏到同一线程之后的请求。新增 `bind_context`,
  把当前上下文带进调用方自建的线程(动态作用域本身不跨线程;那种场景安全上只有 `require_approval` 可靠)。
- 指令 / 数据分通道(ADR 0003):pipeline 上游输出、附件、`$prev` 回灌的工具结果、`AgentTool` /
  `McpClientTool` / `A2AAgentAdapter` 的返回都标为数据(`TaskText`),`SyntaxToolPolicy` 不再把其中的
  `<tool>: <arg>` 当指令执行。
- agent 的产出在**源头**就是数据(ADR 0003):`LlmAgent` / `FunctionAgent` / `ToolUsingAgent` /
  `FunctionCallingAgent` / `ChainAgent` / `MiddlewareAgent` / `DeepResearchAgent` 的 `AgentResult.output` 为整段
  不可信的 `TaskText`(str 子类),调用方直接把它传给下一个 agent 时不再被当指令执行;`DeepResearchAgent` 拼综合
  prompt 时保留各条发现的标记。

### Fixed

- `FunctionCallingAgent`:工具函数抛异常(含 `SkillError`)不再让整轮 `step()` 崩溃,而是归一成
  tool 消息喂回模型(只含稳定错误码 `tool.execution_failed` / CorespineError 的 code 与异常类型名;
  消息原文需 `include_error_message=True`,截断到 300 字符);`fail_fast=True` 保留旧行为;审批错误与
  `KeyboardInterrupt` / `SystemExit` 不吞。
- 历史里坏 JSON 的 tool-call arguments 不再让 Anthropic / Gemini / Bedrock 适配器抛
  `JSONDecodeError`,统一回落为 `{"_raw": <原串>}`。
- `McpClientTool` 遇到缺 `result` 键的结果抛 `McpProtocolError`(code `mcp.invalid_result`)。
- `FunctionCallingAgent` / `ToolUsingAgent` / `DeepResearchAgent` 传入重名工具时构造即抛
  `ValueError`,不再静默覆盖。
- `FailoverProvider` / `StreamingFailoverProvider`:回退与冷却按错误分类,且分类函数可注入
  (`failover_policy`,缺省 `default_failover_policy`)。`retryable` 只表示「对同一家重试有无意义」,与「换一家
  是否可能成功」分开:余额不足 / 配额 / 鉴权 / 模型不存在(含以 400 返回的)回退并**只冷却出错的那一家**;上下文
  超长与无法判定的 4xx 不冷却、**最多再试 1 家**,那家也以同类 4xx 拒绝即抛 `BadRequestProviderError`(一条畸形
  请求最多 2 次计费调用);`BadRequestProviderError` 不回退、不冷却。游标 / 冷却表加锁。
- 各适配器按 vendor 状态码设 `retryable`:非瞬时 4xx(400 / 401 / 403 / 404 / 413 / 422 …)→
  `NonRetryableProviderError`(`retryable=False`);网络 / 超时 / 408 / 425 / 429 / 5xx → `ProviderError(retryable=True)`;
  400 / 422 且 OpenAI 风格错误体明确指出参数校验失败(`invalid_request_error` + `param`,非上下文 / 模型 / 配额类
  code)→ `BadRequestProviderError`。
- `MiddlewareAgent` 步序取号加锁,多线程共享时不重号。
- usage 不再丢失:`FunctionCallingAgent` 多轮累加;`ChainAgent` 累加各段 usage 并拼接 artifacts;
  `AgentTool` 经 `ToolResult` 透传子 agent 的 usage / artifacts,`ToolUsingAgent` 汇总;`DeepResearchAgent` 的
  `usage` 为全部检索 + 综合;`FunctionTool` 函数里嵌套 agent 的 usage 计入外层 `FunctionCallingAgent`(每份只计一次)。
- `CalcTool`:`10.0**400` 之类溢出、语法错误(含括号过深)统一抛 `ValueError`(此前漏出 `OverflowError` /
  `SyntaxError`);深度按真正的嵌套计,超过 100 项的连加 / 连乘不再被拒。
- `InProcessSandbox`:`{**d}` 正确展开(此前得到 `{None: d}`),展开非 dict 判 `disallowed`;调用里的 `**`
  展开明确拒绝(此前 `dict(**d)` 静默得到 `{}`);长的左结合链(`1+…+400`)迭代求值,不再报 `RecursionError`。
- trace 不再记入模型 / 对端可控的自由文本:`FunctionCallingAgent` 的 `tool_step.tool` 只取本地注册表
  里的工具名,未知工具记 `<unknown>`;`A2AAgentAdapter` 的 provenance 与 trace 用本地登记名
  (构造参数 `name=` 或构造期对 `remote.name` 的快照),不再每步读取对端自报名。
- `Coordinator.run_parallel` 可设 `timeout`(整批)/ `task_timeout`(单任务):挂死的 agent 不再卡住
  整批,超时的任务以 `error.code = "orchestration.timeout"` 的结果返回(缺省不限,与旧行为一致)。

### Added

- 审批:`ApprovalGateError`(code `approval.gate_error`)、`ApprovalConfigError`、`UnknownApprovalRequest`、
  `enforce_tool_approval`、`require_approval(tool, gate, *, code=, scope=, sensitive_args=)`、`preflight_tool_approvals`、
  `approval_scope` / `current_approval_scope`、`ConsumableApprovalGate`(`consume`);
  `ApprovalRequest.scope` / `.canonical_arguments` / `.arguments()` / `.preview`;
  `make_approval_request(..., bind_values=, scope=, sensitive_args=)`;
  `ManualApprovalGate(max_requests=, max_pending_per_scope=, request_ttl=, now_fn=)` 与 `resolve(uses=, ttl_seconds=)`;
  `InMemoryResumeTokenStore(max_tokens=, ttl=, now_fn=)`;`ApprovalMiddleware(scope=, sensitive_args=, strict_names=)`;
  `FunctionCallingAgent(approve_before_execute=, on_approval=, max_tool_calls_per_turn=)`;`AgentResult.held_approvals`;
  `StepContext.cleanups` / `StepContext.inner_agent`、`reachable_tool_names` 与各 agent 的 `tool_inventory()`;
  conformance `pending_gate_requires_explicit_scope` / `approval_is_consumed_once` / `approval_is_scope_bound`,
  `ToolExecutionHarness.run(scope=...)`。
- `spineagent.llm.errors.NonRetryableProviderError` / `BadRequestProviderError` / `provider_error_from`;
  `spineagent.llm.failover_provider.FailoverDecision`(`fallback` / `cooldown` / `suspect_request`)/
  `default_failover_policy`、`FailoverProvider(failover_policy=...)` / `make_failover_provider(failover_policy=...)`;
  `spineagent.agent.agent.merge_usage` / `collect_usage` / `report_usage`;`ToolResult.usage` / `ToolResult.artifacts`。
- `Coordinator.run_parallel(timeout=..., task_timeout=..., clock=...)`、`AgentTimeoutError`(不可重试:超时不终止线程)、
  `spineagent.orchestration.coordinator.bind_context`。
- `A2AAgentAdapter(name=...)`;conformance `TOOL_TRACE_INVARIANTS`(未知工具名不进 trace);
  `ToolExecutionHarness.run(trace=...)`。
- `spineagent.agent.trust`:`TaskText` / `untrusted` / `compose` / `lines_with_trust`;
  `SyntaxToolPolicy(parse_untrusted=...)`;`POLICY_INVARIANTS` 新增 `untrusted_data_is_never_an_instruction`。
- `FunctionCallingAgent(fail_fast=..., include_error_message=...)`、`McpProtocolError`、
  `spineagent.tools.tool.index_tools_by_name`。
- `InProcessSandbox(clock=...)`:可注入时钟(默认 `time.monotonic`);`Limits.max_memory_bytes`、
  `ResourceUsage.memory_bytes`。
- conformance:`SANDBOX_INVARIANTS` 新增 `timeout_takes_effect` / `amplifying_expression_is_bounded` /
  `memory_is_bounded`;`APPROVAL_ENFORCEMENT_INVARIANTS` + `ToolExecutionHarness` 协议 + `ScriptedToolCallProvider`
  (离线脚本化 tool_calls 的 provider);`APPROVAL_ENFORCEMENT_INVARIANTS` / `TOOL_TRACE_INVARIANTS` 从顶层
  `spineagent` 导出。

### Documentation

- `docs/llms/`(随 wheel 分发)补齐 sandbox / middleware / artifact / approval / deep research / 信任边界
  与本次行为变化;`api.md` 不再写死过期版本号;README 模块表与 conformance 行补全。
- ADR 0002(审批是工具调用点上的强制闸,含批准语义、显式作用域、至少一次的重跑语义与已知边界)、ADR 0003
  (指令 / 数据分通道,含会丢标记的运算与序列化边界)。
- README / `llms.txt` / `docs/llms/` / `CLAUDE.md` 如实标注 `[mcp]` / `[a2a]` / `[sandbox]` 对应的真实后端、
  `tool_policies["llm"]`、`sandboxes["subprocess"]` 为占位、尚未实现(必抛 `SeamError`)。
- `ci.yml` / `release.yml` / `deploy/README.md` 的 corespine 下限与 `pyproject.toml` 对齐(`>=0.2.0`)。

### Changed

- `ApprovalMiddleware` 不再在 `before_step` 依据 `ctx.tools` 抛错;审批 request id 不再依赖本步
  宣告的工具集,而由工具名 + 完整规范化参数 + 作用域派生。`mw_approval` trace 改为每次受审批调用恰好一条
  (`gated_count=1`)。
- `MiddlewareAgent.step` 无论成败都会逆序执行 `ctx.cleanups`(逐个执行、各自捕获;本步出错时收尾失败只作为
  `__notes__` 附注,本步成功时单个原样抛出、多个抛 `ExceptionGroup`);整步在上下文副本里运行,步内设置的
  contextvar 不再泄漏给调用方。
- `Coordinator.run_parallel` 把调用方的 `contextvars` 上下文复制进每个工作线程。
- `DeepResearchAgent` 遇到审批挂起 / 拒绝时原样上抛,而不是当作一条失败的检索发现继续综合。
- `ApprovalMiddleware` 的受审批名在被包裹 agent 的工具清单里找不到时发一次 `UserWarning`(`strict_names=True` 时报错)。

### Breaking

- 依赖「`ApprovalMiddleware` 在内层 agent 运行前、按 `ctx.tools` 抛 `ApprovalRejected` /
  `ApprovalPending`」的调用方:现在只有在受审批工具**真正被调用**时才抛;内层 agent 不调用该工具
  就不会抛。ManualApprovalGate 上旧的按「工具集」派生的 request id 不再出现,待审请求改为按调用派生。
- **审批批准改为一次性消费**(执行之前核销):此前一次 resolve 后同工具同参数的调用都放行;现在缺省只放行一次,
  要旧行为传 `resolve(..., uses=None)`。
- **会产生待审请求的门必须有显式作用域**:`ManualApprovalGate`(及其它实现 `consume` 的门)配
  `ApprovalMiddleware` / `require_approval` 时,没有 `scope=` 也没有外层 `approval_scope(...)` 即抛
  `ApprovalConfigError`(此前不需要作用域)。恢复须在同一作用域里重跑。
- **审批门改为由门声明是否需要作用域**(`ApprovalGate.requires_scope`,可选属性):`AutoApprovalGate` 声明
  `False`、`ManualApprovalGate` 声明 `True`;**未声明的第三方门按「需要作用域」处理**——没有作用域就抛
  `ApprovalConfigError`、且**不调用**它的 `review`(此前先 `review` 一次看是否返回 `PENDING`,那会在第三方收件箱里
  登记一条无作用域的请求,批准后所有不带作用域的调用方都能共用)。对「未声明的第三方同步门」是行为变化:若该门不会产生
  待审请求,在门上声明 `requires_scope = False`(报错信息里有同样提示)。
- `ManualApprovalGate.resolve` 对未登记 / 已过期的 id 抛 `UnknownApprovalRequest`(此前可预先批准);请求表满时
  `review` 抛 `ApprovalGateError`(fail-closed)。`gated_tools` 含通配符、或与已注册工具仅大小写 / 分隔符不同时抛
  `ApprovalConfigError`(此前静默放行)。
- `ApprovalRequest` 新增字段 `scope` / `canonical_arguments` / `preview`;设了作用域时请求 id 随作用域变化;
  `make_approval_request` 新增关键字参数 `bind_values` / `scope` / `sensitive_args`。
  `ToolExecutionHarness.run` 新增 `scope` 关键字参数。`InMemoryResumeTokenStore.redeem` 兑现即删除记录(重放与未知
  token 同为 `InvalidResumeToken`),超出 `max_tokens` 时最早签发的未兑现 token 失效。
- **`FunctionCallingAgent` 缺省「先审后行」**:同一轮 tool_calls 里有任何受审批调用未获批时,该轮其它(未受审批的)
  工具也不执行(此前排在前面的会先执行);要旧行为传 `approve_before_execute=False`。一轮超过 64 个工具调用时整轮
  不执行(`max_tool_calls_per_turn`)。
- `InProcessSandbox.run(timeout=...)` / `Limits.timeout_seconds` 从「只记录」变为强制:求值超过
  timeout(缺省 `DEFAULT_LIMITS` 为 5 秒)判失败。
- `InProcessSandbox` 新增拒绝规则:`round(x, n)` 要求 `|n|` ≤ 2467(`limit_exceeded`);`int(s)` 要求
  数字串 ≤ 4300 字符(`limit_exceeded`,此前由宿主的 `int_max_str_digits` 判 `error`);`sum` 的 `start`
  只接受数值(`sum(lists, [])` 判 `disallowed`);值的容器嵌套深度 ≤ 100;物化的值估算字节累计超过
  `max_memory_bytes`(缺省 32 MiB)判 `limit_exceeded`;调用里的 `**` 展开判 `disallowed`;解析器栈溢出 /
  `RecursionError` 判 `limit_exceeded`(此前分别冒泡 `MemoryError` / 判 `syntax` 或 `error`)。`max_ops` /
  `ResourceUsage.ops` 的单位从「AST 节点数」改为「工作量单位」(节点 + 按规模折算的大操作),同一表达式的 `ops`
  会变大;`max_ops=None` 不再是无上限(硬上限 100 万单位)。`DEFAULT_LIMITS` 新增 `max_memory_bytes`。
- `CalcTool` 拒绝超过上述上限的表达式(此前会长时间计算或抛 `RecursionError`);语法错误 / 数值溢出改抛
  `ValueError`(此前 `SyntaxError` / `OverflowError`)。
- `FunctionCallingAgent` 缺省不再让工具异常冒泡(需要旧行为传 `fail_fast=True`)。
- `McpClientTool` 缺结果键时抛 `McpProtocolError` 而非 `KeyError`。
- 重名工具从「后者静默覆盖前者」变为构造期 `ValueError`。
- 适配器抛出的 `ProviderError` 现在带 `retryable=True`(此前为类默认 False);非瞬时 4xx 改抛子类
  `NonRetryableProviderError`(仍是 `ProviderError`,code `provider.non_retryable`),能明确识别的参数校验错误抛
  `BadRequestProviderError`。**failover 分类变化**:HTTP 400 / 413 / 422 不再一律「不回退」——与 provider 相关的
  (余额 / 配额 / 鉴权 / 模型)回退并冷却出错的那一家,无法判定的最多再试 1 家;401 / 403 / 404 的 `retryable`
  由 True 改为 False(仍会回退,并冷却出错的那一家)。依赖「400 直接上抛」的调用方改抛 / 改判
  `BadRequestProviderError`,或注入 `failover_policy`。
- `FunctionCallingAgent` 的 `usage` 由「末轮」改为「各轮累加」,并含工具函数里嵌套 agent 的 usage;
  `DeepResearchAgent` 的 `usage` 含检索阶段。
- `AgentTimeoutError.retryable` 为 `False`:`run_parallel` 超时不终止线程,原任务可能仍在后台运行,重试会并发出重复
  副作用。
- 本包 agent 的 `AgentResult.output` 从 plain `str` 变为 `TaskText`(str 子类,相等 / 拼接 / 序列化不受影响);依赖
  「把 A 的产出当指令喂给 B 的 `SyntaxToolPolicy`」的调用方需显式 `SyntaxToolPolicy(parse_untrusted=True)`。
  `type(output) is str` 之类的精确类型判断会变为 False。
- `SyntaxToolPolicy` 缺省不再解析被标为数据的文本:依赖「pipeline 上游输出驱动下游执行工具」的
  调用方需显式 `SyntaxToolPolicy(parse_untrusted=True)`;`SummaryMiddleware` 的摘要也属数据。
