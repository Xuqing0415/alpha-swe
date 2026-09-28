# -*- coding: utf-8 -*-
"""``tui/formatting.py`` 接线统一事件流 schema（EventKind 分组着色）的测试。

覆盖三点：已在 ``_LOG_TYPES`` 登记的类型行为逐字不变；未登记但已知分组
（``EVENT_TYPES`` 命中）的事件按分组着色；完全未知的类型回退 INFO。
"""
import json

import pytest
from rich.text import Text

from agent.observability.event_schema import EventKind, describe, kind_of
from tui.formatting import _KIND_STYLES, _LOG_TYPES, _format_body, format_event

# `[HH:MM:SS] ` 前缀固定 11 列，TYPE 标签紧随其后且右对齐 5 列。
_TAG_START = len("[00:00:00] ")
_TAG_END = _TAG_START + 5


def _tag(text: Text) -> str:
    """取出渲染行里的 TYPE 标签（去掉右对齐补白）。"""
    return text.plain[_TAG_START:_TAG_END].strip()


def _tag_style(text: Text) -> str:
    """取出 TYPE 标签所在的颜色样式（取覆盖标签起始位的 span）。"""
    for span in text.spans:
        if span.start <= _TAG_START < span.end:
            return str(span.style)
    raise AssertionError("未找到 TYPE 标签 span: " + repr(text.plain))


# ---- 已登记类型：样式沿用 _LOG_TYPES，逐字不变 ----

@pytest.mark.parametrize("etype,data,tag,color", [
    ("think", {"content": "分析中"}, "THINK", "cyan"),
    ("tool_call", {"tool": "terminal_execute", "params": {"command": "ls"},
                   "success": True, "output": "a.txt"}, "ACT", "bold white"),
    ("run_error", {"error": "boom"}, "ERROR", "red"),
    ("task_done", {"task_id": "t1"}, "OK", "green"),
    ("session_state", {"kind": "phase", "message": "阶段切换"}, "GATE",
     "magenta"),
    ("budget_warning", {"task_id": "t1", "kind": "tokens", "used": 9,
                        "budget": 10, "pct": 90}, "WARN", "yellow"),
])
def test_已登记类型沿用_LOG_TYPES样式(etype, data, tag, color):
    text = format_event({"type": etype, "data": data})
    assert _LOG_TYPES[etype] == (tag, color)
    assert _tag(text) == tag
    assert _tag_style(text) == color


# ---- 未登记但已知分组：按 EventKind 分组着色 ----

@pytest.mark.parametrize("etype,kind,tag,color", [
    ("phase_barrier_task_start", EventKind.BARRIER, "GATE", "magenta"),
    ("phase_barrier_summary", EventKind.BARRIER, "GATE", "magenta"),
    ("task_retry", EventKind.TASK, "TASK", "green"),
    ("task_skipped", EventKind.TASK, "TASK", "green"),
    ("task_failed", EventKind.TASK, "TASK", "green"),
    ("dry_run_plan", EventKind.LIFECYCLE, "INFO", "bright_black"),
])
def test_未登记已知分组按分组着色(etype, kind, tag, color):
    assert etype not in _LOG_TYPES, "该类型已登记，无法验证分组兜底"
    assert kind_of(etype) is kind
    assert _KIND_STYLES[kind] == (tag, color)
    text = format_event({"type": etype, "data": {"task_id": "t1"}})
    assert _tag(text) == tag
    assert _tag_style(text) == color


def test_KIND_STYLES覆盖全部分组且与既有类型同色():
    assert set(_KIND_STYLES) == set(EventKind)
    assert _KIND_STYLES[EventKind.THOUGHT] == _LOG_TYPES["think"]
    assert _KIND_STYLES[EventKind.TOOL] == _LOG_TYPES["tool_call"]
    assert _KIND_STYLES[EventKind.STATE] == _LOG_TYPES["session_state"]
    assert _KIND_STYLES[EventKind.BUDGET] == _LOG_TYPES["budget_warning"]
    assert _KIND_STYLES[EventKind.ERROR] == _LOG_TYPES["run_error"]
    assert _KIND_STYLES[EventKind.OTHER] == ("INFO", "bright_black")


# ---- 完全未知类型：回退 INFO / bright_black 且不抛异常 ----

@pytest.mark.parametrize("record", [
    {"type": "totally_unknown_event", "data": {"x": 1}},
    {"type": "totally_unknown_event", "data": {}},
    {"type": "totally_unknown_event", "data": "不是字典"},
    {"data": {"x": 1}},
    {"type": None, "data": {"x": 1}},
    {},
])
def test_完全未知类型回退INFO且不抛异常(record):
    text = format_event(record)
    assert isinstance(text, Text)
    assert _tag(text) == "INFO"
    assert _tag_style(text) == "bright_black"


# ---- describe() 兜底正文 ----

@pytest.mark.parametrize("record", [
    {"type": "phase_barrier_task_start", "data": {"task_id": "t7"},
     "ts": 1.0},
    {"type": "task_retry", "data": {"task_id": "t1", "attempt": 2},
     "ts": 2.0},
    {"type": "dry_run_plan", "data": {"total": 3}, "ts": 3.0},
    {"type": "totally_unknown_event", "data": {"x": 1}, "ts": 4.0},
])
def test_未命中具体类型时正文用describe兜底(record):
    expected = describe(record)
    assert expected
    assert "\n" not in expected
    text = format_event(record)
    assert expected in text.plain


def test_format_body记录参数缺省时保持旧兜底():
    data = {"task_id": "t1"}
    assert _format_body("phase_barrier_task_start", data) == (
        "phase_barrier_task_start: " + str(data))


def test_format_body命中具体类型时忽略记录参数():
    record = {"type": "think", "data": {"content": "思考中"}, "ts": 1.0}
    assert _format_body("think", record["data"], record) == "思考中"


# ---- 落盘事件流逐条渲染（真实 JSONL 消费路径） ----

def test_事件流jsonl逐条渲染不抛异常(ws_tmp):
    records = [
        {"type": "phase_barrier_task_start", "data": {"task_id": "t0"},
         "ts": 1.0},
        {"type": "task_skipped", "data": {"task_id": "t1"}, "ts": 2.0},
        {"type": "totally_unknown_event", "data": {"x": 1}, "ts": 3.0},
    ]
    path = ws_tmp / "events.jsonl"
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    path.write_text(body, encoding="utf-8")

    expected = [("GATE", "magenta"), ("TASK", "green"),
                ("INFO", "bright_black")]
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(records)
    for index, line in enumerate(lines):
        text = format_event(json.loads(line))
        tag, color = expected[index]
        assert _tag(text) == tag
        assert _tag_style(text) == color
