"""skills 缝:带 manifest 的可调用能力包(脚本经 Sandbox 隔离执行)。

Protocol + 离线确定性默认(FixtureSkill 进程内 fixture skill)+ Registry 工厂(含 bundle 目录
加载器 + entry-point 第三方发现)+ 参数化 conformance。skill 直接桥成 FunctionTool 进
FunctionCallingAgent(见 skills/as_tool.py)。
"""

from spineagent.skills.as_tool import skill_as_function_tool
from spineagent.skills.bundle import SkillBundle
from spineagent.skills.skill import (
    FixtureSkill,
    Skill,
    SkillError,
    SkillResult,
    SkillSpec,
    skill_registry,
)

__all__ = [
    "Skill",
    "SkillSpec",
    "SkillResult",
    "SkillError",
    "FixtureSkill",
    "SkillBundle",
    "skill_registry",
    "skill_as_function_tool",
]
