"""把一个 Skill 桥成 FunctionTool:让 skill 直接进 FunctionCallingAgent(真 function-calling)。

skill 天然带 inputs JSON-schema(describe() 即 OpenAI function-tool 形状),故桥接是零阻抗的:
schema 直接复用,func 即「用结构化 args 调 skill.invoke、取其 output」。桥出来的 FunctionTool 可
直接进 FunctionCallingAgent 的 tools=,由 LLM 决定何时以何参调用它——skill 的脚本仍经其自带 Sandbox
隔离执行。校验(JSON / required / 类型)由 FunctionTool.parse_arguments 用这份 schema 完成(复用现有
边界校验,不重复造轮子)。
"""

from spineagent.skills.skill import Skill
from spineagent.tools.function_tool import FunctionTool


def skill_as_function_tool(skill: Skill) -> FunctionTool:
    """把一个 Skill 桥成 FunctionTool(名字 / 说明 / 参数 schema 取自 skill.describe())。"""
    function = skill.describe()["function"]
    return FunctionTool(
        name=function["name"],
        description=function.get("description", ""),
        parameters=function["parameters"],
        func=lambda **kwargs: skill.invoke(kwargs).output,
    )
