"""信任边界:把任务文本分成【指令】与【数据】两个通道(见 docs/adr/0003)。

只有调用方直接交给 agent.step 的 task 文本是指令;编排器传给下游的上游输出、附件内容、工具结果、
MCP / A2A 对端返回都是【数据】——它们可以被读、被转述、被交给 LLM,但绝不能被指令语法解析器
(SyntaxToolPolicy 的 `<tool>: <arg>`)当成要执行的工具调用。

表示:TaskText 是 str 的子类,额外携带「不可信字符区间」。对一切只认 str 的代码完全透明(相等、
拼进 prompt、序列化都照旧);只有指令解析器会读这些区间。plain str 一律视为调用方给的指令(向后
兼容)。注意:对 TaskText 做普通 str 运算(拼接 / 切片 / strip)得到的是 plain str,标记会丢——拼接
数据一律用 compose,数据源头一律用 untrusted。
"""

from __future__ import annotations

# 一个不可信区间:[start, end) 字符下标。
type Span = tuple[int, int]


class TaskText(str):
    """带不可信数据区间的任务文本(str 子类;区间之外的字符才可能被当作指令解析)。"""

    untrusted_spans: tuple[Span, ...]

    def __new__(cls, text: str, spans: tuple[Span, ...] = ()) -> TaskText:
        obj = super().__new__(cls, text)
        obj.untrusted_spans = spans
        return obj

    def __reduce__(self) -> tuple[type[TaskText], tuple[str, tuple[Span, ...]]]:
        return (TaskText, (str(self), self.untrusted_spans))


def untrusted(text: str) -> TaskText:
    """把一段文本整体标成【数据】(上游输出 / 附件 / 工具结果 / 对端返回的源头都应过这一步)。"""
    raw = str(text)
    return TaskText(raw, ((0, len(raw)),) if raw else ())


def untrusted_spans(text: str) -> tuple[Span, ...]:
    """取一段文本的不可信区间;plain str 没有(整段都是调用方指令)。"""
    return text.untrusted_spans if isinstance(text, TaskText) else ()


def compose(*parts: str) -> str:
    """按序拼接若干文本并保留各自的不可信区间;全部可信时退回 plain str。"""
    spans: list[Span] = []
    offset = 0
    for part in parts:
        spans.extend((start + offset, end + offset) for start, end in untrusted_spans(part))
        offset += len(part)
    text = "".join(str(part) for part in parts)
    return TaskText(text, tuple(spans)) if spans else text


def lines_with_trust(text: str) -> list[tuple[str, bool]]:
    """按 splitlines 语义切行,并标出每行是否【完全】由可信字符组成(有一个不可信字符即不可信)。"""
    spans = untrusted_spans(text)
    result: list[tuple[str, bool]] = []
    offset = 0
    for raw_line in str(text).splitlines(keepends=True):
        line = raw_line.splitlines()[0] if raw_line.splitlines() else ""
        start, end = offset, offset + len(line)
        trusted = not any(s < end and e > start for s, e in spans) if end > start else True
        result.append((line, trusted))
        offset += len(raw_line)
    return result
