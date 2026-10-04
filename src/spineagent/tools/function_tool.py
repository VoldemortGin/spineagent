"""结构化工具:带 JSON-schema、接 dict 参数的工具(给真 LLM function-calling 用)。

现有 Tool(run(arg: str))是给离线 SyntaxToolPolicy 的单串参工具;真 LLM function-calling 需要把
工具的【名字 + 说明 + 参数 JSON schema】告诉模型,模型回结构化 arguments(dict),再据此执行。
FunctionTool 就是这个:schema() 产出 OpenAI function-tool 形状喂给 chat(tools=...);invoke(args)
用 dict 参数调用底层 Python 函数。

@function_tool 装饰器从普通函数的类型注解 + docstring 自动推 schema(CrewAI / OpenAI SDK 同款 DX),
保持薄:只覆盖常见标量/容器类型,复杂 schema 可显式用 FunctionTool 构造。
"""

import inspect
import json
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

# JSON schema type → 接受的 Python 类型(用工具自带 schema 做最小校验;bool 不算 integer)。
_SCHEMA_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class InvalidToolArguments(ValueError):
    """LLM 发射的工具入参非法(JSON 不可解析 / 不符工具 schema / 不匹配函数签名)。

    边界校验异常:给畸形或敌意载荷一个清晰可定位的错误,而非裸 JSONDecodeError / TypeError。
    携带 `tool`(工具名)便于定位是哪个工具的哪次调用出的问题。
    """

    def __init__(self, tool: str, message: str) -> None:
        self.tool = tool
        super().__init__(f"工具 {tool!r} 入参非法:{message}")


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

    def parse_arguments(self, raw: str) -> dict[str, Any]:
        """把 LLM 发射的 arguments JSON 串解析 + 按本工具自带 schema 校验成可 splat 的 dict。

        边界校验(优先用工具自带 JSON schema):①JSON 必须可解析且是对象;②required 字段必须齐;
        ③声明了 properties 时,不接受 schema 外的多余键(挡敌意/畸形载荷,避免 **kwargs 撞函数
        签名抛裸 TypeError);④声明类型的字段须类型相符。任一不满足抛 InvalidToolArguments
        (清晰可定位),而非裸 JSONDecodeError / TypeError。
        """
        try:
            parsed = json.loads(raw or "{}")
        # JSONDecodeError 是 ValueError 的子类;超过 4300 位的整数等抛的是裸 ValueError。
        except ValueError as exc:
            raise InvalidToolArguments(self.name, f"arguments 不是合法 JSON({exc})") from exc
        if not isinstance(parsed, dict):
            raise InvalidToolArguments(
                self.name, f"arguments 必须是 JSON 对象,实得 {type(parsed).__name__}"
            )

        properties = self.parameters.get("properties")
        required = self.parameters.get("required", [])
        missing = [k for k in required if k not in parsed]
        if missing:
            raise InvalidToolArguments(self.name, f"缺少必填参数 {missing}")
        # 仅在 schema 显式声明 properties 时,才拒绝多余键(无 properties 声明 → 不约束,保持薄)。
        if isinstance(properties, dict):
            unexpected = [k for k in parsed if k not in properties]
            if unexpected:
                raise InvalidToolArguments(self.name, f"含 schema 外的多余参数 {unexpected}")
            for key, spec in properties.items():
                if key not in parsed:
                    continue
                expected = spec.get("type") if isinstance(spec, dict) else None
                if not isinstance(expected, str):
                    continue  # 未声明类型 → 不约束该字段
                accepted = _SCHEMA_PY_TYPES.get(expected)
                if accepted is not None and not _matches(parsed[key], expected, accepted):
                    raise InvalidToolArguments(
                        self.name,
                        f"参数 {key!r} 类型应为 {expected},实得 {type(parsed[key]).__name__}",
                    )
        return parsed

    def invoke(self, arguments: dict[str, Any]) -> str:
        """用模型给的结构化 arguments(dict)调用底层函数,结果转字符串(回填进对话)。"""
        return str(self.func(**arguments))


def _matches(value: Any, expected: str, accepted: tuple[type, ...]) -> bool:
    """value 是否满足 JSON schema 的 expected 类型。

    bool 在 Python 是 int 子类,故 integer/number 要显式排除 bool;boolean 只认 bool。
    """
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, accepted)


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
