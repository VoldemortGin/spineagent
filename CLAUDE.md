# CLAUDE.md — spineagent(宪章)

> 家族关系与依赖：先读 [`docs/spine-family.md`](docs/spine-family.md)（每仓副本相同，真源在家族根目录）。

Spine 家族的 AI / 人类协作契约。先读家族 `../README.md` 与
`../docs/adr/0001-spine-family-boundaries-and-dependency-direction.md`,本文件是 spineagent 的操作指南。

## 这是什么

**spineagent —— 通用多 agent 协作框架**(ADR 0001 D1)。agent / tool / 编排 + MCP / A2A 等
agent 协议缝。它是家族里【演进最快】的成员,刻意单开独立包,**不**拖累薄核与兄弟包。它依赖薄核
`corespine`,复用其缝元模式与 observability / config 形状,但**不**含任何 RAG 概念。

## 宪章(不可违背)

- **离线优先、import-clean、零重依赖默认路径。** 核心默认路径只用标准库 + `corespine`;真实协议
  SDK(MCP / A2A 等)经**可选 extra 延迟 import**(`corespine.lazy_extra_import`),缺 extra 时给
  「pip install spineagent[<extra>]」友好报错。**import spineagent 绝不拉入任何网络 SDK**
  (有 `tests/test_import_clean.py` 把这条钉死)。
- **每条缝长一个样**(家族统一元模式):**Protocol + 离线确定性默认 + Registry 工厂 + 参数化
  conformance**。core 只 import Protocol,绝不 import SDK。
- **机制借 corespine,保证本包自绑**(ADR 0001 D6)。corespine 只给 conformance 基座;具体不变量
  (agent 步 provenance、步级 trace 隐私安全、tool 结果 provenance)由 `spineagent/conformance.py`
  绑定,并用参数化 conformance 测试钉死。
- **不在包层面依赖 ragspine。** 可在【运行时】把 ragspine 当一个 Tool / MCP server 组合调用
  (ADR 0001 D4b),那是松耦合的可选组合,方向只能 spineagent→ragspine,绝不反向,也绝不写进
  `dependencies`。
- **隐私 trace。** 任何步级 / 编排级 trace 只记 code / 计数 / 耗时,绝不记任务或输出正文。

## 模块地图(按文件夹定位)

```
src/spineagent/
  agent/agent.py            Agent 协议 + LlmAgent(走 corespine LLMProvider)/ FunctionAgent(纯函数)
  llm/provider.py           真实 LLM provider 适配器(对外统一 OpenAI chat-completions 形状):OpenAICompatProvider(openai SDK + base_url 吃下所有 OpenAI 兼容端点)+ AnthropicProvider(默认 claude-opus-4-8);二者【额外】实现 StreamingLLMProvider 叠加协议(stream_chat → ChatCompletionChunk,流式拼接 == 非流式)。llm_providers Registry。各走可选 extra 延迟 import,离线默认仍 MockProvider。
  llm/cohere_provider.py    CohereProvider:Cohere v2 native → OpenAI ChatCompletion([cohere] extra)
  llm/gemini_provider.py    GeminiProvider:Gemini generateContent native → OpenAI ChatCompletion([gemini] extra,同覆盖 Vertex Gemini)
  llm/bedrock_provider.py   BedrockConverseProvider:AWS Bedrock Converse native → OpenAI ChatCompletion([bedrock] extra,Converse 跨模型同形)
  llm/failover_provider.py  FailoverProvider / StreamingFailoverProvider:组合式容错——包裹一组下游做轮询分摊 + 撞可重试错(默认 ProviderError)冷却 + 全冷却时强制回退,全失败抛脱敏聚合错(绝不含凭据);只回退可重试错、绝不吞逻辑错;make_failover_provider 仅在全下游支持流式时才声明流式(isinstance 不撒谎);冷却用可注入 now_fn。registry spec "failover" 走 downstreams spec 列表装配
  agent/policy.py           ToolPolicy 缝:协议 + 离线确定性默认 SyntaxToolPolicy(`<tool>: <arg>` 语法路由,不假装 LLM 推理)+ tool_policies Registry
  agent/tool_using.py       ToolUsingAgent:离线确定性多步循环(SyntaxToolPolicy 语法路由),带 max_steps 守卫;实现 Agent 协议
  agent/function_calling.py FunctionCallingAgent:真 LLM function-calling 多步循环(FunctionTool schema → chat(tools=) → tool_calls → 执行 → OpenAI tool 角色喂回 → 再 chat);实现 Agent 协议,底层换任意 provider 不改一行
  agent/as_tool.py          AgentTool:把 Agent 桥成 Tool(分层 / 督导式多 agent:督导 agent 通过工具调用派活给子 agent,可嵌套)
  agent/artifact.py         artifact 缝:Artifact(字节/文本 + mime + name + producer provenance)+ ArtifactSink 协议(store/fetch)+ 离线默认(InProcessArtifactSink / 组合 corespine BlobStore 的 BlobArtifactSink)+ artifact_sinks Registry;AgentResult.artifacts 挂交付物引用
  agent/builtin/deep_research.py  DeepResearchAgent:纯组合(planner 分解 + Coordinator 并行检索 + LlmAgent 综合)装配的预置深研 agent,实现 Agent 协议;离线默认 MockProvider + 空 tools,真实效果靠注入 provider/tools
  agent/middleware.py       Middleware 缝:协议(before_step / after_step)+ MiddlewareAgent 有序洋葱链包裹任意 Agent(组合完仍是 Agent)+ 离线内置四件套(TokenUsage / Summary / DynamicTool / Attachment)+ middlewares Registry;trace 只记计数,零正文泄漏
  agent/approval.py         审批门 / Wait 缝(对标 n8n Send-and-Wait 概念):ApprovalGate 协议(review(ApprovalRequest)→ Decision 三态 approved/rejected/pending)+ ApprovalRequest(只带 code/确定性 id/工具名/参数 schema 指纹+计数,绝不含参数正文)+ 离线默认 AutoApprovalGate(工具名 glob allow/deny 策略表,永不 pending)/ ManualApprovalGate(进程内挂起:review 登记待审、resolve 落决议+铸一次性 resume token、redeem 消费重放必败)+ 可插拔一次性 ResumeTokenStore(默认 InMemoryResumeTokenStore,只存 sha256 哈希)+ make_approval_gate/approval_gates Registry;ApprovalMiddleware 插进 middleware 链:approved 放行、rejected 抛 ApprovalRejected 断路、pending 抛 ApprovalPending 挂起(编排层经 error_to_dict 归一进 AgentResult.error;resolve 后重跑 step 恢复,复用决议幂等,零新增循环机制),默认 gated_tools 空=零行为变化(opt-in);决议幂等,trace 只记 code/计数/决议
  sandbox/seam.py           Sandbox 缝:协议(run(code, *, timeout, limits) -> SandboxResult)+ 离线确定性默认 InProcessSandbox(受限白名单 AST 求值器,构造即保证无网络出口 / 无文件系统逃逸,ops / output 上限)+ sandboxes Registry;真实硬隔离后端 subprocess / container 走 [sandbox] extra 延迟 import
  skills/skill.py           Skill 缝:SkillSpec(manifest)+ 协议(describe() 确定性 schema / invoke(args) 带 provenance)+ 离线默认 FixtureSkill(脚本经 Sandbox 隔离执行)+ skill_registry Registry
  skills/bundle.py          SkillBundle:manifest.toml + 脚本文件的目录加载器(tomllib)
  skills/as_tool.py         skill_as_function_tool:把 Skill 桥成 FunctionTool,直接进 FunctionCallingAgent
  tools/tool.py             Tool 协议 + EchoTool / CalcTool + tool_registry(spec 选工具 + entry-point 第三方工具发现,group corespine.tool);注:运行时可把 ragspine RAG 插为 Tool
  tools/function_tool.py    FunctionTool(带 JSON-schema、接 dict 参数,给真 function-calling 用)+ @function_tool 装饰器(从签名自动推 schema)
  orchestration/coordinator.py  Coordinator:顺序 / 并行 / 流水线(output→input)跑多 agent、保序收集;弹性容错 resilient 把异常归一为 AgentResult.error,坏 agent 不炸整批
  orchestration/chain.py        ChainAgent:把一串 agent 串成单个 Agent(流水线即一等可组合单元:可进 Coordinator / 当工具 / 套 chain)
  protocol/mcp/seam.py      McpClient / McpServer 协议 + OfflineMcpStub(离线回环)+ McpClientTool(MCP 工具→Tool)+ 延迟真实 SDK
  protocol/a2a/seam.py      A2AAgent 协议 + OfflineA2AStub(离线回环)+ A2AAgentAdapter(A2A→Agent)+ 延迟真实 SDK
  conformance.py            本包绑定的不变量包(AGENT / TOOL / POLICY / LLM / STREAMING / SANDBOX / SKILL / MIDDLEWARE / ARTIFACT / APPROVAL_INVARIANTS)
```

## 跑(始终从包根)

```bash
uv venv .venv
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e ../corespine   # 本地兄弟包
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # 期望 GREEN
.venv/bin/python -c "import spineagent"  # 期望 import-clean(无网络 SDK)
```

## 约定

- Python **3.12+** 类型注解;import 顺序 **stdlib > 三方 > 本地**;简体中文 docstring/注释,匹配家族风格。
- **TDD**——测试即规格;**最小改动**——只改需求要求的部分。
- **深层、按领域分组**的布局:文件路径先定位职责,再读文件名。
