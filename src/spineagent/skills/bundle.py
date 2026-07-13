"""SkillBundle:从一个【文件夹包】加载 skill(manifest.toml + 脚本文件)。

一个 skill bundle 的目录布局:

    myskill/
      manifest.toml     # name / description / inputs(JSON-schema object)/ 可选 script 文件名
      skill.py          # 可调用脚本(受限表达式,经 Sandbox 执行);默认文件名 skill.py

manifest.toml 示例:

    name = "add"
    description = "两个整数相加"
    script = "skill.py"          # 可选,默认 "skill.py"

    [inputs]
    type = "object"
    required = ["x", "y"]
    [inputs.properties.x]
    type = "integer"
    [inputs.properties.y]
    type = "integer"

加载后即一个 FixtureSkill:脚本经注入的 Sandbox(默认离线确定性 InProcessSandbox)执行。纯目录
加载器,零数据库、零网络——与家族「纯 registry 缝」一致。
"""

import sys
from pathlib import Path
from typing import Any

from spineagent.sandbox.seam import Sandbox
from spineagent.skills.skill import FixtureSkill, Skill, SkillSpec, skill_registry

# TOML 解析:3.11+ 用标准库 tomllib;3.10 回退到轻量纯 Python 的 tomli(见 pyproject 条件依赖)。
if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover —— 仅 3.10 走此分支;CI 在 3.11+ 上跑
    import tomli as tomllib

_MANIFEST_NAME = "manifest.toml"
_DEFAULT_SCRIPT = "skill.py"


class SkillBundle:
    """skill 文件夹包的目录加载器(manifest.toml + 脚本文件 → FixtureSkill)。"""

    @staticmethod
    def load(path: str | Path, *, sandbox: Sandbox | None = None) -> Skill:
        """从目录 path 加载一个 skill:读 manifest.toml + 脚本文件,构造 FixtureSkill。"""
        base = Path(path)
        manifest_path = base / _MANIFEST_NAME
        if not manifest_path.is_file():
            raise FileNotFoundError(f"skill bundle 缺少 {_MANIFEST_NAME}:{manifest_path}")
        with manifest_path.open("rb") as fh:
            manifest: dict[str, Any] = tomllib.load(fh)

        missing = [k for k in ("name", "description", "inputs") if k not in manifest]
        if missing:
            raise ValueError(f"manifest.toml 缺少必填字段 {missing}:{manifest_path}")

        script_name = manifest.get("script", _DEFAULT_SCRIPT)
        script_path = base / script_name
        if not script_path.is_file():
            raise FileNotFoundError(f"skill bundle 缺少脚本文件 {script_name}:{script_path}")
        script = script_path.read_text(encoding="utf-8").strip()

        spec = SkillSpec(
            name=manifest["name"],
            description=manifest["description"],
            inputs=manifest["inputs"],
        )
        return FixtureSkill(spec, script, sandbox=sandbox)


# 注册表 'bundle' 位:skill_registry.make("bundle", path=<目录>) 即从目录加载。
skill_registry.register("bundle", lambda **kw: SkillBundle.load(**kw))
