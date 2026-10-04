"""门声明作用域要求(requires_scope):不再先调第三方门的 review 去探测——那会在第三方收件箱里登记一条无作用域的请求。"""

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
    ApprovalMiddleware,
    ApprovalRequest,
    AutoApprovalGate,
    Decision,
    ManualApprovalGate,
    require_approval,
)
from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.agent.middleware import MiddlewareAgent
from spineagent.tools.function_tool import FunctionTool


class _Delete:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def tool(self) -> FunctionTool:
        def delete_file(value: str) -> str:
            self.calls.append(value)
            return "deleted"

        return FunctionTool(
            "delete_file",
            "",
            {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            func=delete_file,
        )


class _OneCall:
    def chat(self, messages, *, tools=None):
        if not any(m.get("role") == "assistant" for m in messages):
            call = LLMToolCall(
                id="c0",
                function=FunctionCall(name="delete_file", arguments=json.dumps({"value": "/a"})),
            )
            message = ResponseMessage(role="assistant", content=None, tool_calls=(call,))
            return ChatCompletion(choices=(Choice(index=0, message=message),))
        message = ResponseMessage(role="assistant", content="done")
        return ChatCompletion(choices=(Choice(index=0, message=message),))


class _ExternalInbox:
    """第三方门(转发给外部收件箱):未声明 requires_scope;记录 review 调用——每次 review 都是一条登记。"""

    name = "external"

    def __init__(self, decision: Decision = Decision.PENDING) -> None:
        self.decision = decision
        self.reviews: list[ApprovalRequest] = []

    def review(self, request: ApprovalRequest) -> Decision:
        self.reviews.append(request)
        return self.decision


class _DeclaredSync(_ExternalInbox):
    requires_scope = False


def _fc(delete: _Delete, **kw) -> FunctionCallingAgent:
    return FunctionCallingAgent("fc", _OneCall(), [delete.tool()], **kw)


def test_builtin_gates_declare_their_scope_requirement():
    assert AutoApprovalGate.requires_scope is False
    assert ManualApprovalGate().requires_scope is True


def test_undeclared_third_party_gate_without_scope_is_a_config_error_and_never_reviewed_middleware():
    delete, gate = _Delete(), _ExternalInbox()
    agent = MiddlewareAgent(
        "mw", _fc(delete), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
    )
    with pytest.raises(ApprovalConfigError) as ei:
        agent.step("go")
    assert "requires_scope = False" in str(ei.value)
    assert gate.reviews == []  # 没有在第三方收件箱里登记任何无作用域请求
    assert delete.calls == []


def test_undeclared_third_party_gate_without_scope_is_a_config_error_and_never_reviewed_wrapper():
    delete, gate = _Delete(), _ExternalInbox(Decision.APPROVED)
    tool = require_approval(delete.tool(), gate)
    agent = FunctionCallingAgent("fc", _OneCall(), [tool])
    with pytest.raises(ApprovalConfigError):
        agent.step("go")
    assert gate.reviews == [] and delete.calls == []


def test_undeclared_third_party_gate_with_a_scope_works_as_before():
    delete, gate = _Delete(), _ExternalInbox(Decision.APPROVED)
    agent = MiddlewareAgent(
        "mw", _fc(delete), [ApprovalMiddleware(gate, gated_tools=["delete_file"], scope="S")]
    )
    agent.step("go")
    assert delete.calls == ["/a"]
    assert gate.reviews and all(r.scope == "S" for r in gate.reviews)


def test_gate_declaring_requires_scope_false_needs_no_scope():
    delete, gate = _Delete(), _DeclaredSync(Decision.APPROVED)
    MiddlewareAgent(
        "mw", _fc(delete), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
    ).step("go")
    assert delete.calls == ["/a"]


@pytest.mark.parametrize("junk", ["no", 0, None])
def test_non_bool_declaration_is_treated_as_requiring_a_scope(junk):
    class Sloppy(_ExternalInbox):
        requires_scope = junk

    delete, gate = _Delete(), Sloppy()
    agent = MiddlewareAgent(
        "mw", _fc(delete), [ApprovalMiddleware(gate, gated_tools=["delete_file"])]
    )
    with pytest.raises(ApprovalConfigError):
        agent.step("go")
    assert gate.reviews == []
