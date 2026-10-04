# spineagent — recipes

可运行最小示例,**离线优先**(默认走 `corespine.MockProvider`,零网络、确定性可复现)。每段都能
原样粘进 Python 跑。最后一节示范怎么接真实 LLM 后端(只示范接法,不真联网)。

> 前置:`pip install spineagent`(自动带上 `corespine`)。真实后端另需对应 extra,见末节。

---

## 1) 建并跑一个 agent(离线 MockProvider)

```python
from corespine import MockProvider
from spineagent import LlmAgent, FunctionAgent

# 走 corespine 确定性 MockProvider:对同一对话恒定产出 '[mock:<hex>] <最后一条 user 文本>'
planner = LlmAgent("planner", MockProvider(), system="你是计划助手")
r = planner.step("列个三步计划")
print(r.agent, "->", r.output)         # planner -> [mock:...] 列个三步计划
print(r.usage)                          # {'prompt_tokens': ..., 'completion_tokens': ..., 'total_tokens': ...}

# 纯函数 agent(无需 LLM,做编排 / 测试节点)
rev = FunctionAgent("reverse", lambda t: t[::-1])
print(rev.step("abc").output)          # cba
```

---

## 2) 工具:直接调 + 缝注册表选

```python
from spineagent import EchoTool, CalcTool, tool_registry

print(EchoTool().run("hi").tool)            # echo (provenance)
print(CalcTool().run("2 * (3 + 4)").output) # 14

# 按 spec 选工具;支持第三方经 entry-point group "corespine.tool" 自动发现
print(tool_registry.names())                # ['calc', 'echo']（names() 排序返回）
print(tool_registry.make("calc").run("6/2").output)   # 3
```

---

## 3) 会用工具的多步循环(离线确定性 ToolUsingAgent)

`SyntaxToolPolicy` 按任务文本里的 `<tool>: <arg>` 语法确定性路由;`$prev` 在执行前替换为上一步输出。

```python
from spineagent import ToolUsingAgent, SyntaxToolPolicy, CalcTool

solver = ToolUsingAgent("solver", SyntaxToolPolicy(), [CalcTool()])
# 第一步 calc: 2+3 = 5;第二步 calc: $prev * 2 把 5 喂回 = 10
print(solver.step("calc: 2 + 3\ncalc: $prev * 2").output)   # 10
```

---

## 4) 顺序 / 并行 / 流水线编排(Coordinator)

```python
from spineagent import Coordinator, FunctionAgent

coord = Coordinator([
    FunctionAgent("a", lambda t: f"a:{t}"),
    FunctionAgent("b", lambda t: f"b:{t}"),
])

print([r.output for r in coord.run_sequential("go")])  # ['a:go', 'b:go']
print([r.output for r in coord.run_parallel("go")])    # ['a:go', 'b:go'](并发,仍保序)
print([r.output for r in coord.run_pipeline("go")])    # ['a:go', 'b:a:go'](链式:上一个输出喂下一个)
```

弹性容错:坏 agent 不炸整批,异常归一为结构化 `error`。

```python
flaky = Coordinator([
    FunctionAgent("ok", lambda t: t),
    FunctionAgent("bad", lambda t: 1 / 0),
])
for r in flaky.run_sequential("go", resilient=True):
    print(r.agent, r.ok, (r.error or {}).get("code"))
# ok True None
# bad False error            (error 是 corespine.errors.error_to_dict 的归一 dict)
```

---

## 5) 流水线即一等 agent(ChainAgent)

```python
from spineagent import ChainAgent, FunctionAgent, Coordinator

etl = ChainAgent("etl", [
    FunctionAgent("extract", lambda t: f"extract({t})"),
    FunctionAgent("transform", lambda t: t.upper()),
    FunctionAgent("load", lambda t: f"load[{t}]"),
])
print(etl.step("raw").output)          # load[EXTRACT(RAW)]

# ChainAgent 本身是 Agent,可再进编排
print([r.output for r in Coordinator([etl]).run_sequential("raw")])  # ['load[EXTRACT(RAW)]']
```

---

## 6) 分层 / 督导式多 agent(AgentTool)

把子 agent 用 `AgentTool` 暴露成工具,督导 agent 通过工具调用派活给它(可嵌套)。

```python
from spineagent import ToolUsingAgent, SyntaxToolPolicy, CalcTool, AgentTool

calculator = ToolUsingAgent("calculator", SyntaxToolPolicy(), [CalcTool()])
supervisor = ToolUsingAgent("supervisor", SyntaxToolPolicy(), [AgentTool(calculator)])
# 督导派给子 agent calculator,子 agent 再用 calc 工具:2+3=5
print(supervisor.step("calculator: calc: 2+3").output)   # 5
```

---

## 7) MCP 离线回环 + 桥成 Tool

```python
from spineagent import OfflineMcpStub, McpTool, McpClientTool, ToolUsingAgent, SyntaxToolPolicy

stub = OfflineMcpStub()
# 处理器:dict 进、dict 出
stub.register_tool(McpTool("upper"), lambda a: {"result": a["input"].upper()})
print(stub.call_tool("upper", {"input": "hi"}))   # {'result': 'HI'}

# 把该 MCP 工具桥成 Tool,交给会用工具的 agent 在循环里驱动(零网络)
# McpClientTool 默认 arg_key="input"、result_key="result",与上面处理器匹配
shouter = ToolUsingAgent("shouter", SyntaxToolPolicy(), [McpClientTool("upper", stub)])
print(shouter.step("upper: hi").output)           # HI
```

---

## 8) A2A 离线回环 + 桥成 Agent

```python
from spineagent import OfflineA2AStub, A2AAgentAdapter, Coordinator

remote = OfflineA2AStub(name="worker", responder=lambda text: f"done:{text}")
print(remote.card())                              # {'name': 'worker', 'transport': 'offline-loopback', 'skills': ['echo']}

# 把远端 A2A agent 桥成本地 Agent,像本地 agent 一样进编排
local = A2AAgentAdapter(remote)
print(local.name)                                 # worker
print([r.output for r in Coordinator([local]).run_sequential("task-x")])   # ['done:task-x']
```

---

## 9) 隐私安全 trace(只记元数据)

```python
from corespine import MockProvider, InProcessPrivacyTraceSink
from spineagent import LlmAgent

sink = InProcessPrivacyTraceSink()
LlmAgent("planner", MockProvider()).step("敏感任务正文", trace=sink)
for e in sink.events:
    print(e.code, dict(e.fields))
# agent_step {'agent': 'planner', 'task_chars': 6, 'output_chars': 26, 'input_tokens': 11, 'output_tokens': 26}
# 只有 code / 计数 / 长度,绝无任务或输出正文。塞正文会被 corespine 的 sink 直接抛 TraceError。
```

---

## 10) 真 LLM function-calling 的循环(FunctionCallingAgent)

`@function_tool` 从函数签名自动推 schema。`FunctionCallingAgent` 把 schema 喂给 `model.chat(tools=...)`,
模型回 `tool_calls` 则执行→喂回→再 chat。**离线 `MockProvider` 不回 tool_calls**,所以下面用一个
脚本化 fake「模型」离线演示完整循环(真实后端见末节):

```python
from corespine.llm.provider import (
    ChatCompletion, Choice, FunctionCall, ResponseMessage, ToolCall,
)
from spineagent import FunctionCallingAgent, function_tool


@function_tool
def add(a: int, b: int) -> int:
    """两个整数相加。"""
    return a + b


class ScriptedModel:
    """第 1 轮要调 add(2,3),第 2 轮出最终文本——无网络演示 function-calling 循环。"""
    def __init__(self):
        self._turns = iter([
            ChatCompletion(choices=(Choice(0, ResponseMessage(
                "assistant", None,
                (ToolCall("c1", FunctionCall("add", '{"a": 2, "b": 3}')),)), "tool_calls"),)),
            ChatCompletion(choices=(Choice(0, ResponseMessage("assistant", "答案是 5")),)),
        ])
    def chat(self, messages, *, tools=None):
        return next(self._turns)


agent = FunctionCallingAgent("solver", ScriptedModel(), [add])
print(agent.step("2+3 等于几?").output)   # 答案是 5(模型调了 add(2,3) 拿到 5 后作答)
```

> 注意:`@function_tool` 产出的是 `FunctionTool`(给 `FunctionCallingAgent` 用),**不能**直接丢进
> `ToolUsingAgent`(后者要的是实现了 `Tool.run(str)` 的工具,如 `CalcTool` / `McpClientTool` / `AgentTool`)。

---

## 11) 接真实 LLM 后端(只示范接法,需联网 + key)

对外统一 OpenAI `ChatCompletion` 形状,`LlmAgent` / `FunctionCallingAgent` / 编排只认 `LLMProvider`
协议——把 `MockProvider` 换成真实适配器,其余代码一行不改。先装对应 extra:

```bash
pip install "spineagent[openai]"      # OpenAI 及一切「OpenAI 兼容」端点
pip install "spineagent[anthropic]"   # 或 [cohere] / [gemini] / [bedrock]
pip install "spineagent[all]"         # 装齐全部 LLM + 协议后端
```

```python
from spineagent import (
    OpenAICompatProvider, AnthropicProvider, GeminiProvider, LlmAgent,
)

# 一个适配器吃下所有 OpenAI 兼容端点(换 base_url + model 即可):
gpt = LlmAgent("gpt", OpenAICompatProvider("gpt-4o"))                     # 官方 OpenAI
local = LlmAgent("local", OpenAICompatProvider("llama3", base_url="http://localhost:11434/v1"))  # Ollama
groq = LlmAgent("groq", OpenAICompatProvider("llama-3.1-70b", base_url="https://api.groq.com/openai/v1"))

# 非 OpenAI 原生模型:原生适配器在内部转成 OpenAI 形状,用户无感
claude = LlmAgent("claude", AnthropicProvider())                          # 默认 claude-opus-4-8
gemini = LlmAgent("gemini", GeminiProvider(model="gemini-2.5-flash"))

# 用法与离线完全一致:
# print(gpt.step("写一句问候").output)

# 也可走缝注册表按名建(等价):
from spineagent import llm_providers
# provider = llm_providers.make("openai", model="gpt-4o")
print(llm_providers.names())   # ['anthropic', 'bedrock', 'cohere', 'failover', 'gemini', 'mock', 'openai']（排序返回）
```

> API key 等凭据走各 SDK 的默认环境变量(如 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`),或经
> `**client_kwargs` / `client=` 注入。未装对应 extra 而去构造真实适配器,会得到「pip install
> spineagent[<extra>]」的友好 ImportError。

## 12) 审批:风险工具调用前停下等人(离线脚本化 provider)

```python
from spineagent import (
    ApprovalMiddleware, ApprovalPending, Decision, FunctionCallingAgent,
    ManualApprovalGate, MiddlewareAgent, function_tool,
)
from spineagent.conformance import ScriptedToolCallProvider  # 离线:按脚本回 tool_calls

deleted = []

@function_tool
def delete_file(path: str) -> str:
    """删除一个文件。"""
    deleted.append(path)
    return "deleted"

gate = ManualApprovalGate()
model = ScriptedToolCallProvider([("delete_file", {"path": "/tmp/a"})], final="完成")
agent = MiddlewareAgent(
    "ops",
    FunctionCallingAgent("worker", model, [delete_file]),
    [ApprovalMiddleware(gate, gated_tools=["delete_file"])],
)

try:
    agent.step("清理临时文件")
except ApprovalPending as exc:            # 真实工具调用前被拦下:delete_file 一次都没执行
    request_id = exc.context["request_id"]
print(deleted)                            # []

token = gate.resolve(request_id, Decision.APPROVED)   # out-of-band 批准(收件箱 / 管理台)
gate.redeem(token)                                    # 一次性 resume token,重放必败
print(agent.step("清理临时文件").output)   # '完成'(同工具 + 同参数命中已落决议)
print(deleted)                            # ['/tmp/a']
```

参数被改(如 `/etc`)就是新请求,会再次 `ApprovalPending`。需要不依赖上下文的保证时,用
`require_approval(delete_file, gate)` 把闸绑进工具本身。

## 13) 信任边界:上游输出是数据

```python
from spineagent import (
    A2AAgentAdapter, CalcTool, Coordinator, OfflineA2AStub, SyntaxToolPolicy, ToolUsingAgent,
)

peer = A2AAgentAdapter(OfflineA2AStub(name="peer", responder=lambda t: "报告如下\ncalc: 9**9"))
downstream = ToolUsingAgent("down", SyntaxToolPolicy(), [CalcTool()])
results = Coordinator([peer, downstream]).run_pipeline("hi")
print(results[-1].output)   # '报告如下\ncalc: 9**9'——作为正文流转,但没有被当成指令执行

# 确认上游可信、确实要它驱动下游时,显式打开旧行为:
legacy = ToolUsingAgent("down", SyntaxToolPolicy(parse_untrusted=True), [CalcTool()])
print(Coordinator([peer, legacy]).run_pipeline("hi")[-1].output)  # 末行是 calc 的结果
```

