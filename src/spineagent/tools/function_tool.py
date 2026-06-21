"""结构化工具:带 JSON-schema、接 dict 参数的工具(给真 LLM function-calling 用)。

现有 Tool(run(arg: str))是给离线 SyntaxToolPolicy 的单串参工具;真 LLM function-calling 需要把
工具的【名字 + 说明 + 参数 JSON schema】告诉模型,模型回结构化 arguments(dict),再据此执行。
FunctionTool 就是这个:schema() 产出 OpenAI function-tool 形状喂给 chat(tools=...);invoke(args)
用 dict 参数调用底层 Python 函数。

@function_tool 装饰器从普通函数的类型注解 + docstring 自动推 schema(CrewAI / OpenAI SDK 同款 DX),
保持薄:只覆盖常见标量/容器类型,复杂 schema 可显式用 FunctionTool 构造。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Python 注解 → JSON schema type(未识别一律落 string,够用即止)。
_JSON_TYPES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class FunctionTool:
    """一个可被 LLM function-calling 的工具:名字 + 说明 + 参数 JSON schema + 底层函数。"""

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]

    def schema(self) -> dict[str, Any]:
        """OpenAI function-tool 形状(直接喂给 LLMProvider.chat(tools=...))。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def invoke(self, arguments: dict[str, Any]) -> str:
        """用模型给的结构化 arguments(dict)调用底层函数,结果转字符串(回填进对话)。"""
        return str(self.func(**arguments))


def _schema_from_signature(func: Callable[..., Any]) -> dict[str, Any]:
    """从函数签名 + 类型注解推一个最小 JSON-schema(无默认值的参数为 required)。"""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in inspect.signature(func).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        json_type = _JSON_TYPES.get(param.annotation, "string")
        properties[pname] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    return {"type": "object", "properties": properties, "required": required}


def function_tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Any:
    """装饰器:把一个普通函数包成 FunctionTool(schema 从签名/注解/docstring 自动推)。

    用法:@function_tool 直接装,或 @function_tool(name=..., description=...) 覆盖。
    """

    def wrap(f: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(
            name=name or f.__name__,
            description=description or (inspect.getdoc(f) or "").strip(),
            parameters=_schema_from_signature(f),
            func=f,
        )

    return wrap(func) if func is not None else wrap
