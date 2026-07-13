"""artifact 缝的单元测试:Artifact 文本/字节 + 两个 sink round-trip + 内容寻址 + 挂在 AgentResult。

conformance 已把「ref 溯源 sink + 保留产出者 provenance + round-trip」参数化钉死(见 test_conformance.py);
这里补 Artifact 的文本视图、BlobArtifactSink 组合 BlobStore、内容寻址去重、artifacts 挂 AgentResult。
"""

import tempfile

from corespine.blob.store import FileSystemBlobStore, MemoryBlobStore

from spineagent.agent.agent import AgentResult, FunctionAgent
from spineagent.agent.artifact import (
    Artifact,
    ArtifactRef,
    BlobArtifactSink,
    InProcessArtifactSink,
    artifact_sinks,
)


def test_artifact_from_text_round_trips_text_view():
    art = Artifact.from_text("a.txt", "你好", producer="agent-x")
    assert art.data == "你好".encode()
    assert art.text == "你好"
    assert art.mime == "text/plain" and art.producer == "agent-x"


def test_in_process_sink_stores_and_fetches():
    sink = InProcessArtifactSink()
    ref = sink.store(Artifact.from_text("n.txt", "hi", producer="p"))
    assert isinstance(ref, ArtifactRef)
    assert ref.sink == "in_process" and ref.producer == "p" and ref.size == 2
    assert sink.fetch(ref).text == "hi"


def test_blob_sink_composes_memory_blobstore():
    store = MemoryBlobStore()
    sink = BlobArtifactSink(store)
    ref = sink.store(Artifact(name="b.bin", data=b"\x00\x01\x02", mime="application/octet-stream"))
    # 字节确实落进了底层 blob(内容寻址键)。
    assert store.exists(ref.key)
    assert sink.fetch(ref).data == b"\x00\x01\x02"


def test_blob_sink_persists_across_instances_on_filesystem():
    with tempfile.TemporaryDirectory() as root:
        ref = BlobArtifactSink(FileSystemBlobStore(root)).store(
            Artifact.from_text("keep.txt", "persist", producer="w")
        )
        # 同 root 新 sink 实例据 ref 取回同字节 + 元数据(跨实例确定映射)。
        got = BlobArtifactSink(FileSystemBlobStore(root)).fetch(ref)
        assert got.text == "persist" and got.producer == "w"


def test_content_addressing_dedups_same_bytes():
    sink = InProcessArtifactSink()
    r1 = sink.store(Artifact.from_text("x", "same"))
    r2 = sink.store(Artifact.from_text("y", "same"))
    assert r1.key == r2.key  # 同内容同键


def test_registry_makes_both_sinks():
    assert artifact_sinks.make("in_process").name == "in_process"
    assert artifact_sinks.make("blob", store=MemoryBlobStore()).name == "blob"
    assert {"in_process", "blob"} <= set(artifact_sinks.names())


def test_agent_result_carries_artifact_refs():
    ref = InProcessArtifactSink().store(Artifact.from_text("r.txt", "deliverable", producer="a"))
    result = AgentResult(agent="a", output="done", artifacts=(ref,))
    assert result.artifacts == (ref,)
    # 默认无 artifact 的 agent 无感:空 tuple。
    assert FunctionAgent("f", lambda t: "x").step("go").artifacts == ()
