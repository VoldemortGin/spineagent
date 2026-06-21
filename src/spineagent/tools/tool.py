"""tool 缝:Tool 协议 + 两个玩具工具(echo / calc)。

一个 Tool 是 agent 在一步里可调用的能力:给一段输入参数,拿回一个带 provenance 的结果
(ToolResult.tool 标明产出它的工具,可溯源)。两个默认工具离线可跑、零依赖。

运行时组合(ADR 0001 D4b):可把 ragspine 的 RAG 当作一个 Tool 插在【这里】——让某个
agent 通过本协议在运行时调用 ragspine 做检索。但 spineagent【不】依赖 ragspine:那是
松耦合的运行时组合(import 一个实现了 Tool 协议的适配器即可),不是包依赖,方向也只是
可选的 spineagent→ragspine,绝不反向。
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from corespine.seam.registry import Registry


@dataclass(frozen=True)
class ToolResult:
    """工具调用结果:产出文本 + 来源工具名(provenance,可溯源到产出它的工具)。"""

    tool: str
    output: str


@runtime_checkable
class Tool(Protocol):
    """tool 协议:有名字;给一段输入参数,拿回一个带 provenance 的结果。"""

    name: str

    def run(self, arg: str) -> ToolResult: ...


class EchoTool:
    """玩具工具:原样回显输入(最小的「能力」示例)。"""

    name = "echo"

    def run(self, arg: str) -> ToolResult:
        return ToolResult(tool=self.name, output=arg)


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class CalcTool:
    """玩具工具:安全求值一个算术表达式(只认数字与 +-*/%**,绝不 eval 任意代码)。"""

    name = "calc"

    def run(self, arg: str) -> ToolResult:
        value = _safe_eval(ast.parse(arg, mode="eval").body)
        # 整数值去掉多余的 .0,输出更干净。
        text = (
            str(int(value))
            if isinstance(value, float) and value.is_integer()
            else str(value)
        )
        return ToolResult(tool=self.name, output=text)


def _safe_eval(node: ast.AST) -> float:
    """递归求值一棵【白名单】算术 AST;遇到任何非算术节点即拒绝(不触碰任意代码执行)。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"不支持的表达式节点:{type(node).__name__}")


# 工具缝注册表:一个 spec 选实现 + entry-point 第三方工具【自动发现】(group "corespine.tool")。
# 这正是家族「敢放手让第三方填广度、却让脊柱不变量(TOOL_INVARIANTS)烂不掉」的落点:第三方
# 装个包、在 "corespine.tool" entry-point group 下注册自己的工具工厂,即可被 tool_registry.make /
# names 发现并组合进 agent,无需改本包代码;而它们仍须过 conformance 的 TOOL_INVARIANTS 才算数。
# 命名注:变量叫 tool_registry 而非 tools,以避开与 spineagent.tools 子包同名(其余缝无此冲突)。
tool_registry: Registry[Tool] = Registry("tool")
tool_registry.register("echo", lambda **kw: EchoTool(**kw))
tool_registry.register("calc", lambda **kw: CalcTool(**kw))
