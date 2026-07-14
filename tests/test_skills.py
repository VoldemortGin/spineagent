"""skills 缝的单元测试:FixtureSkill 契约 + 沙箱隔离 + FunctionTool 桥 + bundle 加载 + 注册表。

conformance 已把「describe() 确定性 + invoke provenance / 产出非空」参数化钉死(见 test_conformance.py);
这里补 skill 专属行为:脚本经沙箱隔离(越权抛 SkillError)、桥进 FunctionCallingAgent、目录加载。
"""

import pytest
from corespine.llm.provider import MockProvider

from spineagent.agent.function_calling import FunctionCallingAgent
from spineagent.skills.as_tool import skill_as_function_tool
from spineagent.skills.bundle import (
    MAX_INPUT_NAME_CHARS,
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_NAME_CHARS,
    SkillBundle,
)
from spineagent.skills.skill import (
    FixtureSkill,
    SkillError,
    SkillResult,
    SkillSpec,
    skill_registry,
)

_ADD_SPEC = SkillSpec(
    name="add",
    description="两个整数相加",
    inputs={
        "type": "object",
        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
        "required": ["x", "y"],
    },
)


def _add_skill() -> FixtureSkill:
    return FixtureSkill(_ADD_SPEC, "x + y")


def _write_raw_bundle(tmp_path, manifest: str) -> None:
    (tmp_path / "manifest.toml").write_text(manifest, encoding="utf-8")
    (tmp_path / "skill.py").write_text("1 + 1", encoding="utf-8")


def test_describe_is_openai_function_schema():
    schema = _add_skill().describe()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "add"
    assert schema["function"]["parameters"] == _ADD_SPEC.inputs


def test_invoke_runs_script_through_sandbox_with_provenance():
    result = _add_skill().invoke({"x": 2, "y": 5})
    assert isinstance(result, SkillResult)
    assert result.output == "7"
    assert result.skill == "add"  # provenance
    assert result.usage is not None and result.usage.ops > 0  # 沙箱资源记账透传


def test_script_escape_is_isolated_and_raises_skill_error():
    # skill 脚本经沙箱执行:越权代码被沙箱判失败,invoke 抛 SkillError(而非静默成功)。
    evil = FixtureSkill(
        SkillSpec("evil", "试图越权", {"type": "object", "properties": {}}),
        "__import__('os')",
    )
    with pytest.raises(SkillError) as exc:
        evil.invoke({})
    assert exc.value.skill == "evil"
    assert exc.value.reason == "disallowed"


def test_skill_bridges_to_function_tool():
    ft = skill_as_function_tool(_add_skill())
    assert ft.name == "add"
    assert ft.schema()["function"]["parameters"] == _ADD_SPEC.inputs
    # 桥出来的 FunctionTool 用结构化参数调用底层 skill(经沙箱),取其 output。
    assert ft.invoke({"x": 3, "y": 4}) == "7"


def test_skill_function_tool_plugs_into_function_calling_agent():
    # 直接进 FunctionCallingAgent 的 tools=(离线 MockProvider 不 function-call,故只验装配无碍 +
    # 产出非空;真调用路径由 test_function_calling.py 覆盖)。
    ft = skill_as_function_tool(_add_skill())
    agent = FunctionCallingAgent("fc", MockProvider(), [ft])
    result = agent.step("加一下")
    assert result.output


def test_bundle_loads_skill_from_directory(tmp_path):
    (tmp_path / "manifest.toml").write_text(
        "\n".join(
            [
                'name = "greet"',
                'description = "打招呼"',
                'script = "skill.py"',
                "",
                "[inputs]",
                'type = "object"',
                'required = ["name"]',
                "[inputs.properties.name]",
                'type = "string"',
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "skill.py").write_text("f'hi {name}'", encoding="utf-8")

    skill = SkillBundle.load(tmp_path)
    assert skill.spec.name == "greet"
    assert skill.invoke({"name": "bob"}).output == "hi bob"


def test_bundle_default_script_name(tmp_path):
    (tmp_path / "manifest.toml").write_text(
        'name = "one"\ndescription = "常量"\n\n[inputs]\ntype = "object"\n',
        encoding="utf-8",
    )
    (tmp_path / "skill.py").write_text("1 + 1", encoding="utf-8")
    assert SkillBundle.load(tmp_path).invoke({}).output == "2"


@pytest.mark.parametrize(
    ("name_line", "description_line", "inputs_block"),
    [
        ("name = 1", 'description = "ok"', '[inputs]\ntype = "object"'),
        ('name = "   "', 'description = "ok"', '[inputs]\ntype = "object"'),
        (
            'name = "' + "n" * (MAX_SKILL_NAME_CHARS + 1) + '"',
            'description = "ok"',
            '[inputs]\ntype = "object"',
        ),
        ('name = "ok"', "description = 2", '[inputs]\ntype = "object"'),
        ('name = "ok"', 'description = "  "', '[inputs]\ntype = "object"'),
        (
            'name = "ok"',
            'description = "' + "d" * (MAX_SKILL_DESCRIPTION_CHARS + 1) + '"',
            '[inputs]\ntype = "object"',
        ),
        ('name = "ok"', 'description = "ok"', 'inputs = "bad"'),
        ('name = "ok"', 'description = "ok"', '[inputs]\ntype = "array"'),
        (
            'name = "ok"',
            'description = "ok"',
            '[inputs]\ntype = "object"\nproperties = []',
        ),
        (
            'name = "ok"',
            'description = "ok"',
            '[inputs]\ntype = "object"\nproperties = { "" = { type = "string" } }',
        ),
        (
            'name = "ok"',
            'description = "ok"',
            '[inputs]\ntype = "object"\n'
            + 'properties = { "'
            + "p" * (MAX_INPUT_NAME_CHARS + 1)
            + '" = '
            '{ type = "string" } }',
        ),
        (
            'name = "ok"',
            'description = "ok"',
            '[inputs]\ntype = "object"\nrequired = "x"',
        ),
        (
            'name = "ok"',
            'description = "ok"',
            '[inputs]\ntype = "object"\nrequired = ["missing"]',
        ),
        (
            'name = "ok"',
            'description = "ok"',
            '[inputs]\ntype = "object"\nrequired = ["x", "x"]\n'
            '[inputs.properties.x]\ntype = "string"',
        ),
        (
            'name = "ok"',
            'description = "ok"',
            '[inputs]\ntype = "object"\nproperties = { x = "bad" }',
        ),
    ],
)
def test_bundle_rejects_invalid_manifest_schema(
    tmp_path, name_line, description_line, inputs_block
):
    _write_raw_bundle(
        tmp_path,
        f"{name_line}\n{description_line}\n{inputs_block}\n",
    )

    with pytest.raises(ValueError, match="manifest"):
        SkillBundle.load(tmp_path)


def test_bundle_missing_manifest_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        SkillBundle.load(tmp_path)


@pytest.mark.parametrize("script_ref", ["../outside.py", "../../outside.py"])
def test_bundle_rejects_script_path_outside_bundle(tmp_path, script_ref):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("1 + 1", encoding="utf-8")
    (tmp_path / "manifest.toml").write_text(
        "\n".join(
            [
                'name = "escape"',
                'description = "越界读取"',
                f'script = "{script_ref}"',
                "",
                "[inputs]",
                'type = "object"',
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bundle"):
        SkillBundle.load(tmp_path)


def test_bundle_rejects_absolute_script_path(tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("1 + 1", encoding="utf-8")
    (tmp_path / "manifest.toml").write_text(
        "\n".join(
            [
                'name = "absolute"',
                'description = "绝对路径读取"',
                f'script = "{outside.as_posix()}"',
                "",
                "[inputs]",
                'type = "object"',
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bundle"):
        SkillBundle.load(tmp_path)


def test_registry_makes_fixture_and_bundle(tmp_path):
    # 注册表 'fixture' 位收扁平 manifest 字段(不收 spec=,避与 Registry.make(spec,…) 撞名)。
    skill = skill_registry.make(
        "fixture",
        name="add",
        description="两个整数相加",
        inputs=_ADD_SPEC.inputs,
        script="x + y",
    )
    assert skill.invoke({"x": 1, "y": 1}).output == "2"
    assert {"fixture", "bundle"} <= set(skill_registry.names())
