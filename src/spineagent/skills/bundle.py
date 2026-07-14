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
MAX_SKILL_NAME_CHARS = 128
MAX_SKILL_DESCRIPTION_CHARS = 4_096
MAX_INPUT_NAME_CHARS = 128
MAX_INPUT_PROPERTIES = 256


def _manifest_text(value: object, *, label: str, max_chars: int) -> str:
    """校验 manifest 的有界非空普通字符串字段。"""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")
    if len(value) > max_chars:
        raise ValueError(f"{label} 长度不能超过 {max_chars}")
    return value


def _manifest_inputs(value: object) -> dict[str, Any]:
    """校验最小 object JSON-schema 形状，避免畸形 tool face 流入宿主。"""
    if type(value) is not dict or value.get("type") != "object":
        raise ValueError("manifest.inputs 必须是 type=object 的 table")
    properties = value.get("properties")
    property_names: set[str] = set()
    if properties is not None:
        if type(properties) is not dict:
            raise ValueError("manifest.inputs.properties 必须是 table")
        if len(properties) > MAX_INPUT_PROPERTIES:
            raise ValueError(f"manifest.inputs.properties 不能超过 {MAX_INPUT_PROPERTIES} 项")
        for name, schema in properties.items():
            checked_name = _manifest_text(
                name,
                label="manifest.inputs.properties 键",
                max_chars=MAX_INPUT_NAME_CHARS,
            )
            if type(schema) is not dict:
                raise ValueError("manifest.inputs.properties 的值必须是 table")
            property_names.add(checked_name)
    required = value.get("required")
    if required is not None:
        if type(required) is not list or len(required) > MAX_INPUT_PROPERTIES:
            raise ValueError("manifest.inputs.required 必须是有界字符串数组")
        seen_required: set[str] = set()
        for name in required:
            checked_name = _manifest_text(
                name,
                label="manifest.inputs.required 项",
                max_chars=MAX_INPUT_NAME_CHARS,
            )
            if checked_name in seen_required or checked_name not in property_names:
                raise ValueError("manifest.inputs.required 必须无重复且引用 properties 中的键")
            seen_required.add(checked_name)
    return value


def _bundle_file(base: Path, relative: object, *, label: str) -> Path:
    """解析 bundle 内文件，拒绝绝对路径、越界与符号链接。"""
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError(f"{label} 必须是 bundle 内的非空相对路径")
    rel = Path(relative)
    if rel.is_absolute():
        raise ValueError(f"{label} 必须位于 bundle 内，不允许绝对路径")
    base_resolved = base.resolve()
    candidate = base_resolved / rel
    cursor = base_resolved
    for part in rel.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"{label} 必须是 bundle 内普通文件，不允许符号链接")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        raise ValueError(f"{label} 必须位于 bundle 内，不允许路径越界") from None
    # 即便链接最终仍落在 bundle 内，也拒绝它：bundle 文件应是自包含的普通文件，避免
    # 安装/复制阶段与加载阶段看到不同目标（TOCTOU）或由链接逃逸宿主文件系统。
    if not resolved.is_file():
        raise FileNotFoundError(f"skill bundle 缺少普通文件 {relative!r}")
    return resolved


class SkillBundle:
    """skill 文件夹包的目录加载器(manifest.toml + 脚本文件 → FixtureSkill)。"""

    @staticmethod
    def load(path: str | Path, *, sandbox: Sandbox | None = None) -> Skill:
        """从目录 path 加载一个 skill:读 manifest.toml + 脚本文件,构造 FixtureSkill。"""
        base = Path(path).resolve()
        manifest_path = base / _MANIFEST_NAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise FileNotFoundError(f"skill bundle 缺少 {_MANIFEST_NAME}:{manifest_path}")
        with manifest_path.open("rb") as fh:
            manifest: dict[str, Any] = tomllib.load(fh)

        missing = [k for k in ("name", "description", "inputs") if k not in manifest]
        if missing:
            raise ValueError(f"manifest.toml 缺少必填字段 {missing}:{manifest_path}")

        script_name = manifest.get("script", _DEFAULT_SCRIPT)
        script_path = _bundle_file(base, script_name, label="manifest.script")
        script = script_path.read_text(encoding="utf-8").strip()

        spec = SkillSpec(
            name=_manifest_text(
                manifest["name"],
                label="manifest.name",
                max_chars=MAX_SKILL_NAME_CHARS,
            ),
            description=_manifest_text(
                manifest["description"],
                label="manifest.description",
                max_chars=MAX_SKILL_DESCRIPTION_CHARS,
            ),
            inputs=_manifest_inputs(manifest["inputs"]),
        )
        return FixtureSkill(spec, script, sandbox=sandbox)


# 注册表 'bundle' 位:skill_registry.make("bundle", path=<目录>) 即从目录加载。
skill_registry.register("bundle", lambda **kw: SkillBundle.load(**kw))
