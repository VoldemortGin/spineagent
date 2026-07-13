"""artifact 缝:Artifact 类型 + ArtifactSink 协议 + 离线默认(进程内 / 组合 corespine BlobStore)。

家族缝的元模式:Protocol + 离线确定性默认 + Registry 工厂 + 参数化 conformance。让 agent 能产出
【文件级交付物】:一个 Artifact 是一段带 mime / 名字 / provenance(哪个 agent / tool 产出)的字节
(或文本)内容;ArtifactSink 把它落地,回一个轻量的 ArtifactRef(带存储 provenance,可据以取回)。

【为何把 artifacts 挂在 AgentResult 上(而非 middleware / extras)】ArtifactRef 是【轻量的、带
provenance 的元数据引用】(重字节留在 sink 里),与 AgentResult 已有的 usage / error 同类——把
它作为 AgentResult 的一等字段,使「这步产出了哪些文件级交付物」可发现、可溯源、类型明确,远胜
埋进无类型的 extras。默认空 tuple,向后兼容:不产 artifact 的 agent 无感。

两个离线默认纯标准库 + corespine、确定性:
  - InProcessArtifactSink —— 进程内 dict,零落地(测试 / 临时);
  - BlobArtifactSink      —— 组合一个 corespine BlobStore(memory / filesystem / 第三方 S3…),
                             字节存进 blob、元数据随 ArtifactRef 返回,取回时据 ref 重建 Artifact。
"""

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from corespine.blob.store import BlobStore
from corespine.seam.registry import Registry

# 缺省 mime:未声明类型的二进制流。
_DEFAULT_MIME = "application/octet-stream"


@dataclass(frozen=True)
class Artifact:
    """一个文件级交付物:字节内容 + mime + 名字 + provenance(产出它的 agent / tool 名)。

    字节为规范存储形态(文本经 from_text 编码进来);.text 按 UTF-8 解码取回文本视图。
    """

    name: str
    data: bytes
    mime: str = _DEFAULT_MIME
    producer: str = ""

    @classmethod
    def from_text(
        cls, name: str, text: str, *, mime: str = "text/plain", producer: str = ""
    ) -> "Artifact":
        """从文本构造 Artifact(UTF-8 编码进字节;mime 默认 text/plain)。"""
        return cls(name=name, data=text.encode("utf-8"), mime=mime, producer=producer)

    @property
    def text(self) -> str:
        """按 UTF-8 解码的文本视图。"""
        return self.data.decode("utf-8")


@dataclass(frozen=True)
class ArtifactRef:
    """落地后的引用:存储键 + 存储 sink 名(存储 provenance)+ 随行元数据(名字 / mime / 产出者 / 大小)。

    轻量、可序列化:重字节留在 sink,ref 只带够「取回 + 溯源」的元数据。sink 标明落在哪条 sink,
    producer 把 Artifact 的产出者 provenance 一路带下去。
    """

    key: str
    sink: str
    name: str
    mime: str
    producer: str
    size: int


@runtime_checkable
class ArtifactSink(Protocol):
    """artifact 出口协议:有名字;落地一个 Artifact 回 ArtifactRef;据 ref 取回 Artifact。"""

    @property
    def name(self) -> str: ...

    def store(self, artifact: Artifact) -> ArtifactRef: ...

    def fetch(self, ref: ArtifactRef) -> Artifact: ...


def _content_key(artifact: Artifact) -> str:
    """内容寻址键:sha256(data) 的 hex——同内容同键(去重)、确定性、跨进程稳定。"""
    return hashlib.sha256(artifact.data).hexdigest()


def _ref_for(sink_name: str, key: str, artifact: Artifact) -> ArtifactRef:
    return ArtifactRef(
        key=key,
        sink=sink_name,
        name=artifact.name,
        mime=artifact.mime,
        producer=artifact.producer,
        size=len(artifact.data),
    )


class InProcessArtifactSink:
    """进程内 dict 实现:内容寻址存 Artifact,零落地(测试 / 临时交付物用)。"""

    name = "in_process"

    def __init__(self) -> None:
        self._items: dict[str, Artifact] = {}

    def store(self, artifact: Artifact) -> ArtifactRef:
        key = _content_key(artifact)
        self._items[key] = artifact
        return _ref_for(self.name, key, artifact)

    def fetch(self, ref: ArtifactRef) -> Artifact:
        return self._items[ref.key]


class BlobArtifactSink:
    """组合一个 corespine BlobStore 的实现:字节落进 blob,元数据随 ArtifactRef 返回。

    字节用内容寻址键存进注入的 BlobStore(memory / filesystem / 第三方 S3…);取回时据 ref 的元数据
    (名字 / mime / 产出者)+ 从 blob 读回的字节重建 Artifact——BlobStore 只承诺字节 round-trip,
    元数据由 ArtifactRef 承载,故无需 blob 侧 sidecar。这正是「组合薄核原语拼出领域能力」的范例。
    """

    def __init__(self, store: BlobStore, *, name: str = "blob") -> None:
        self._store = store
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def store(self, artifact: Artifact) -> ArtifactRef:
        key = _content_key(artifact)
        self._store.put(key, artifact.data)
        return _ref_for(self._name, key, artifact)

    def fetch(self, ref: ArtifactRef) -> Artifact:
        data = self._store.get(ref.key)
        return Artifact(name=ref.name, data=data, mime=ref.mime, producer=ref.producer)


# 缝注册表:一个 spec 选实现(in_process 进程内默认;blob 组合一个 BlobStore,需 store= 注入)。
# 第三方亦可经 entry-point group "corespine.artifact_sink" 装包发现自己的落地后端。
artifact_sinks: Registry[ArtifactSink] = Registry("artifact_sink")
artifact_sinks.register("in_process", lambda **kw: InProcessArtifactSink(**kw))
artifact_sinks.register("blob", lambda **kw: BlobArtifactSink(**kw))
