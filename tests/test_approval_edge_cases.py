"""审批的边界问题(复审第四轮低优先级):作用域早检、孤立代理字符、sensitive_args 校验 / 路径 / 不附摘要、
作用域组合、strict_names 警告文案、超大整数参数。

安全相关一律走真实执行路径:真实 agent + 带副作用计数的真实工具函数 + 离线脚本化 provider。
"""

import json

import pytest
from corespine.llm.provider import (
    ChatCompletion,
    Choice,
    FunctionCall,
    ResponseMessage,
)
from corespine.llm.provider import ToolCall as LLMToolCall

from spineagent.agent.approval import (
    ApprovalConfigError,
    ApprovalGateError,
    ApprovalMiddleware,
    ApprovalPending,
    AutoApprovalGate,
    Decision,
    ManualApprovalGate,
    approval_scope,
    make_approval_request,
    require_approval,
)
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.agent.middleware import MiddlewareAgent
from spineagent.agent.policy import SyntaxToolPolicy
from spineagent.agent.tool_using import ToolUsingAgent
from spineagent.tools.function_tool import FunctionTool, InvalidToolArguments
from spineagent.tools.tool import ToolResult


class _Log:
    """带副作用计数的真实工具函数:每执行一次记一笔(工具名, 参数)。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def tool(self, name: str) -> FunctionTool:
        def run(value: str) -> str:
            self.calls.append((name, value))
            return f"{name}:ok"

        return FunctionTool(
            name,
            "",
            {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            func=run,
        )


class _Script:
    """每轮回放一批 (工具名, 原始 arguments JSON 串) 的离线 provider(无状态,按 assistant 条数定位)。"""

    def __init__(self, rounds) -> None:
        self._rounds = rounds
        self.tool_messages: list[str] = []

    def chat(self, messages, *, tools=None):
        index = sum(1 for m in messages if m.get("role") == "assistant")
        self.tool_messages = [m["content"] for m in messages if m.get("role") == "tool"]
        if index < len(self._rounds):
            calls = tuple(
                LLMToolCall(id=f"c{index}_{j}", function=FunctionCall(name=n, arguments=a))
                for j, (n, a) in enumerate(self._rounds[index])
            )
            message = ResponseMessage(role="assistant", content=None, tool_calls=calls)
            return ChatCompletion(choices=(Choice(index=0, message=message),))
        message = ResponseMessage(role="assistant", content="done")
        return ChatCompletion(choices=(Choice(index=0, message=message),))


def _arg(value: str) -> str:
    return json.dumps({"value": value})


# ---- 作用域要求在 run 开始时检查,早于任何工具执行 ------------------------------------------------


def test_function_calling_agent_checks_wrapper_scopes_before_any_tool_runs():
    log, gate = _Log(), ManualApprovalGate()
    tools = [log.tool("send_email"), require_approval(log.tool("delete_file"), gate)]
    script = _Script([[("send_email", _arg("a"))], [("delete_file", _arg("b"))]])
    with pytest.raises(ApprovalConfigError):
        FunctionCallingAgent("fc", script, tools).step("go")
    assert log.calls == []  # 第一轮的 send_email 也没有执行


def test_function_calling_agent_wrapper_scope_from_outer_scope_is_fine():
    log, gate = _Log(), ManualApprovalGate()
    tools = [log.tool("send_email"), require_approval(log.tool("delete_file"), gate)]
    script = _Script([[("send_email", _arg("a"))], [("delete_file", _arg("b"))]])
    with approval_scope("S"), pytest.raises(ApprovalPending):
        FunctionCallingAgent("fc", script, tools).step("go")
    assert log.calls == [("send_email", "a")]  # 缺作用域才早检;有作用域照常逐轮执行直到挂起


def test_tool_using_agent_checks_wrapper_scopes_before_any_tool_runs():
    hits: list[str] = []

    class Send:
        name = "send"

        def run(self, arg):
            hits.append(arg)
            return ToolResult(tool=self.name, output="sent")

    class Nuke:
        name = "nuke"

        def run(self, arg):
            hits.append(arg)
            return ToolResult(tool=self.name, output="boom")

    agent = ToolUsingAgent(
        "tu", SyntaxToolPolicy(), [Send(), require_approval(Nuke(), ManualApprovalGate())]
    )
    with pytest.raises(ApprovalConfigError):
        agent.step("send: a\nnuke: b")
    assert hits == []


# ---- 孤立代理字符:归一为 ApprovalGateError(fail-closed),消息不含参数内容 -----------------------


@pytest.mark.parametrize("mode", ["raise", "feed_back"])
def test_lone_surrogate_in_gated_arguments_fails_closed(mode):
    log = _Log()
    script = _Script([[("delete_file", '{"value": "\\ud800secret"}')]])
    fc = FunctionCallingAgent("fc", script, [log.tool("delete_file")], on_approval=mode)
    agent = MiddlewareAgent(
        "mw", fc, [ApprovalMiddleware(ManualApprovalGate(), gated_tools=["delete_file"], scope="S")]
    )
    with pytest.raises(ApprovalGateError) as ei:
        agent.step("go")
    assert log.calls == []
    text = str(ei.value) + repr(ei.value.context)
    assert "secret" not in text and "\ud800" not in text


# ---- sensitive_args:类型校验、路径语法、不附摘要 -------------------------------------------------


def test_sensitive_args_given_as_a_str_is_rejected_at_construction():
    with pytest.raises(ApprovalConfigError):
        ApprovalMiddleware(AutoApprovalGate(), gated_tools=["pay"], sensitive_args={"pay": "to"})
    with pytest.raises(ApprovalConfigError):
        require_approval(_Log().tool("pay"), AutoApprovalGate(), sensitive_args="to")
    with pytest.raises(ApprovalConfigError):
        make_approval_request("tool_call", "pay", {"to": "x"}, sensitive_args="to")
    with pytest.raises(ApprovalConfigError):
        make_approval_request("tool_call", "pay", {"to": "x"}, sensitive_args=[""])


def _preview_text(**kw) -> str:
    request = make_approval_request("tool_call", "pay", bind_values=True, scope="S", **kw)
    return " ".join(f"{k}={v}" for k, v in request.preview)


def test_masking_goes_through_lists_and_supports_dotted_keys_and_tuple_paths():
    args = {
        "items": [{"token": "PIN-1111", "n": 1}, {"token": "PIN-2222", "n": 2}],
        "a.b": "DOT-SECRET",
        "body": {"x.y": "DEEP-SECRET", "keep": "visible"},
        "pin": "1234",
    }
    text = _preview_text(
        arguments=args, sensitive_args=["items.token", "a.b", ("body", "x.y"), "pin"]
    )
    for secret in ("PIN-1111", "PIN-2222", "DOT-SECRET", "DEEP-SECRET", "1234"):
        assert secret not in text
    assert "visible" in text  # 没声明的照常显示


def test_masked_values_carry_no_digest_so_low_entropy_values_cannot_be_guessed():
    request = make_approval_request(
        "tool_call", "login", {"pin": "1234", "pwd": "hunter2"}, sensitive_args=["pin", "pwd"]
    )
    rendered = dict(request.preview)
    assert rendered["pin"] == rendered["pwd"] == "***"
    assert "sha256" not in str(request.preview)


# ---- 作用域组合:静态绑定的全局作用域不让不同用户共用批准 ------------------------------------------


def _user_agent(log: _Log, gate, user: str) -> MiddlewareAgent:
    tool = require_approval(log.tool("delete_file"), gate, scope="global")
    fc = FunctionCallingAgent("fc", _Script([[("delete_file", _arg("/a"))]]), [tool])
    return MiddlewareAgent(
        "mw", fc, [ApprovalMiddleware(gate, gated_tools=["delete_file"], scope=user)]
    )


def test_wrapper_scope_composes_with_middleware_scope_instead_of_overriding_it():
    log, gate = _Log(), ManualApprovalGate()
    with pytest.raises(ApprovalPending) as ei:
        _user_agent(log, gate, "alice").step("go")
    gate.resolve(ei.value.context["request_id"], Decision.APPROVED)
    with pytest.raises(ApprovalPending):  # bob 用不了 alice 的批准
        _user_agent(log, gate, "bob").step("go")
    assert log.calls == []
    _user_agent(log, gate, "alice").step("go")  # alice 自己的批准照常放行一次
    assert log.calls == [("delete_file", "/a")]


def test_wrapper_scope_composes_with_outer_approval_scope():
    log, gate = _Log(), ManualApprovalGate()
    tool = require_approval(log.tool("delete_file"), gate, scope="global")
    agent = FunctionCallingAgent("fc", _Script([[("delete_file", _arg("/a"))]]), [tool])
    with approval_scope("alice"), pytest.raises(ApprovalPending) as ei:
        agent.step("go")
    gate.resolve(ei.value.context["request_id"], Decision.APPROVED)
    with approval_scope("bob"), pytest.raises(ApprovalPending):
        agent.step("go")
    assert log.calls == []
    with approval_scope("alice"):
        agent.step("go")
    assert log.calls == [("delete_file", "/a")]


def test_composed_scopes_are_unambiguous():
    log, gate = _Log(), ManualApprovalGate()
    tool = require_approval(log.tool("delete_file"), gate, scope="b/c")
    agent = FunctionCallingAgent("fc", _Script([[("delete_file", _arg("/a"))]]), [tool])
    with approval_scope("a"), pytest.raises(ApprovalPending) as first:
        agent.step("go")
    with approval_scope("a/b"), pytest.raises(ApprovalPending) as second:
        agent.step("go")
    assert first.value.context["request_id"] != second.value.context["request_id"]


# ---- strict_names=False:写错的名字只警告,但要明说「对应的工具不受审批保护」 -----------------------


def test_unmatched_name_warning_says_the_tool_is_unprotected():
    log = _Log()
    fc = FunctionCallingAgent("fc", _Script([]), [log.tool("delete_file")])
    agent = MiddlewareAgent(
        "mw", fc, [ApprovalMiddleware(AutoApprovalGate(deny=["*"]), gated_tools=["rm"])]
    )
    with pytest.warns(UserWarning, match="不受审批保护"):
        agent.step("go")


# ---- 超大整数参数:归一为 InvalidToolArguments 路径 ------------------------------------------------


def test_huge_integer_argument_is_an_invalid_tool_arguments_not_a_value_error():
    tool = _Log().tool("pay")
    with pytest.raises(InvalidToolArguments):
        tool.parse_arguments('{"value": ' + "9" * 5000 + "}")


def test_huge_integer_argument_is_fed_back_to_the_model_and_the_tool_does_not_run():
    log = _Log()
    script = _Script([[("pay", '{"value": ' + "9" * 5000 + "}")]])
    result = FunctionCallingAgent("fc", script, [log.tool("pay")]).step("go")
    assert result.output == "done" and log.calls == []
    assert script.tool_messages and script.tool_messages[0].startswith("error:")
