"""skill 缝:Skill 协议 + 离线确定性默认(带 manifest 的可调用能力包)。

家族缝的元模式:Protocol + 离线确定性默认 + Registry 工厂 + 参数化 conformance。一个 skill 是
一份【manifest(name / description / inputs schema)+ 可调用脚本】的能力包:describe() 把它的
调用契约(inputs JSON-schema)确定性地暴露出来,invoke(args) 用结构化参数执行、拿回带 provenance
的结果。skill 天然带 inputs schema,故【直接桥成 FunctionTool 进 FunctionCallingAgent】(见
skills/as_tool.py)——skill 就是给真 function-calling agent 用的「能力」。

【为何脚本经 Sandbox 执行】skill 脚本是外来代码,直接在进程内 exec 即失控。故 skill 的脚本一律
经 ② 的 Sandbox 执行(默认离线确定性 InProcessSandbox 受限白名单求值器):隔离由沙箱构造即保证,
skill 只负责「把 args 当 env 喂进去、把沙箱结果包成带 provenance 的 SkillResult」。离线默认
FixtureSkill 即「进程内 fixture skill」:manifest + 一段受限表达式脚本,零网络、确定性、可复现。
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from corespine.errors import SeamError
from corespine.seam.registry import Registry

from spineagent.sandbox.seam import InProcessSandbox, ResourceUsage, Sandbox


class SkillError(RuntimeError):
    """skill 脚本在沙箱内执行失败(语法 / 越权 / 触限 / 运行时错误)时抛出。

    携带 skill 名与沙箱失败原因,便于定位是哪个 skill 的哪次调用出的问题。
    """

    def __init__(self, skill: str, reason: str, detail: str) -> None:
        self.skill = skill
        self.reason = reason
        super().__init__(f"skill {skill!r} 执行失败({reason}):{detail}")


@dataclass(frozen=True)
class SkillSpec:
    """一个 skill 的 manifest:名字 + 说明 + inputs JSON-schema(object,声明可传哪些结构化参数)。

    inputs 用标准 JSON-schema object 形状({"type":"object","properties":{...},"required":[...]}),
    与 FunctionTool.parameters 同构——故 describe() 可零成本产出 OpenAI function-tool schema。
    """

    name: str
    description: str
    inputs: dict[str, Any]


@dataclass(frozen=True)
class SkillResult:
    """一次 skill 调用的结果:产出文本 + 来源 skill 名(provenance)+ 可选沙箱资源记账。"""

    skill: str
    output: str
    usage: ResourceUsage | None = None


@runtime_checkable
class Skill(Protocol):
    """skill 协议:有 manifest(spec);describe() 确定性给出调用契约;invoke(args) 带 provenance 执行。"""

    spec: SkillSpec

    def describe(self) -> dict[str, Any]: ...

    def invoke(self, args: dict[str, Any]) -> SkillResult: ...


def _function_schema(spec: SkillSpec) -> dict[str, Any]:
    """把 SkillSpec 规整成 OpenAI function-tool schema(确定性纯函数;直接可喂 FunctionTool)。"""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.inputs,
        },
    }


class FixtureSkill:
    """离线确定性默认 skill:manifest + 一段【受限表达式脚本】,脚本经 Sandbox 执行(默认 InProcessSandbox)。

    invoke(args):把 args 当沙箱 env(脚本里的自由名即取自 args)、跑脚本、把沙箱产出包成带 provenance
    的 SkillResult。沙箱失败(越权 / 触限 / 语法 / 运行时错)则抛 SkillError(清晰可定位),而非把失败
    伪装成成功产出。describe() 确定性:同一 spec 恒定同一 schema。
    """

    def __init__(self, spec: SkillSpec, script: str, *, sandbox: Sandbox | None = None) -> None:
        self.spec = spec
        self._script = script
        # 默认离线确定性 InProcessSandbox;可注入 subprocess / container 等真实硬隔离后端。
        self._sandbox = sandbox if sandbox is not None else InProcessSandbox()

    def describe(self) -> dict[str, Any]:
        return _function_schema(self.spec)

    def invoke(self, args: dict[str, Any]) -> SkillResult:
        result = self._sandbox.run(self._script, env=args)
        if not result.ok:
            raise SkillError(self.spec.name, result.error or "error", result.output)
        return SkillResult(skill=self.spec.name, output=result.output, usage=result.usage)


def _make_fixture_skill(**kwargs: Any) -> Skill:
    # 注册表工厂收【扁平 manifest 字段】(name / description / inputs / script,+ 可选 sandbox),
    # 内部拼成 SkillSpec——刻意不收 spec= 关键字,以规避与 Registry.make(spec, **kw) 的形参撞名。
    required = {"name", "description", "inputs", "script"}
    missing = required - kwargs.keys()
    if missing:
        raise SeamError(
            f"构造 fixture skill 缺少字段 {sorted(missing)}:需传 name / description / inputs / "
            "script(+ 可选 sandbox);离线默认脚本经 InProcessSandbox 执行。"
        )
    spec = SkillSpec(
        name=kwargs["name"], description=kwargs["description"], inputs=kwargs["inputs"]
    )
    return FixtureSkill(spec, kwargs["script"], sandbox=kwargs.get("sandbox"))


# 缝注册表:一个 spec 选实现(默认 fixture 进程内确定性 skill;bundle 由 skills/bundle.py 注册的
# 目录加载器)。第三方亦可经 entry-point group "corespine.skill" 装包发现自己的 skill 工厂。
skill_registry: Registry[Skill] = Registry("skill")
skill_registry.register("fixture", _make_fixture_skill)
