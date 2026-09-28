# -*- coding: utf-8 -*-
"""统一事件流 schema（agent/observability/event_schema.py）的单元测试。"""
import copy
import json

import pytest

from agent.observability.event_schema import (EVENT_TYPES, REQUIRED_FIELDS,
                                              EventKind, describe, is_valid,
                                              kind_of, known_types,
                                              normalize_event, to_sse,
                                              validate_event)


# ---- normalize_event ----

def test_normalize_event_不修改入参():
    record = {"type": "tool_call",
              "data": {"tool": "file_ops", "task_id": "t0"}, "ts": 111.0}
    original = copy.deepcopy(record)
    event = normalize_event(record)
    assert record == original
    assert event is not record
    assert event["data"] is not record["data"]


def test_normalize_event_保留额外的顶层键():
    record = {"type": "run_start", "data": {}, "ts": 1.0, "seq": 7}
    assert normalize_event(record)["seq"] == 7


def test_normalize_event_补齐缺省字段():
    for raw in ({}, None, "不是 dict", [1, 2, 3], 42):
        event = normalize_event(raw)
        assert event["type"] == "unknown"
        assert event["data"] == {}
        assert isinstance(event["ts"], float)


def test_normalize_event_非法type归为unknown():
    for raw in ("", "   ", 123, None, ["think"]):
        event = normalize_event({"type": raw, "data": {}, "ts": 1.0})
        assert event["type"] == "unknown"


def test_normalize_event_非法ts回退当前时间():
    for raw in (True, "刚刚", None, [1.0]):
        event = normalize_event({"type": "think", "data": {}, "ts": raw})
        assert isinstance(event["ts"], float)


@pytest.mark.parametrize("raw", ["file_ops", ["a", "b"], 0, None])
def test_normalize_event_data非dict包装为value(raw):
    event = normalize_event({"type": "tool_call", "data": raw, "ts": 1.0})
    assert event["data"] == {"value": raw}


def test_normalize_event_缺失data补空字典():
    assert normalize_event({"type": "run_start", "ts": 1.0})["data"] == {}


# ---- kind_of / 枚举分组 ----

@pytest.mark.parametrize("event_type,expected", [
    ("run_start", EventKind.LIFECYCLE),
    ("run_done", EventKind.LIFECYCLE),
    ("run_error", EventKind.LIFECYCLE),
    ("interrupt", EventKind.LIFECYCLE),
    ("priority_changed", EventKind.LIFECYCLE),
    ("task_done", EventKind.TASK),
    ("task_interrupted", EventKind.TASK),
    ("think", EventKind.THOUGHT),
    ("tool_call", EventKind.TOOL),
    ("session_state", EventKind.STATE),
    ("budget_exhausted", EventKind.BUDGET),
    ("phase_barrier_summary", EventKind.BARRIER),
    ("从未登记的类型", EventKind.OTHER),
    ("", EventKind.OTHER),
])
def test_kind_of_已知与未知分组(event_type, expected):
    assert kind_of(event_type) is expected


def test_kind_of_非字符串归入other():
    assert kind_of(None) is EventKind.OTHER
    assert kind_of(["run_start"]) is EventKind.OTHER


def test_event_kind_取值():
    assert EventKind.LIFECYCLE == "lifecycle"
    assert EventKind.OTHER == "other"


# ---- validate_event / is_valid ----

def test_validate_event_合法记录无问题():
    record = {"type": "run_start", "data": {"prompt": "hi"}, "ts": 1.5}
    assert validate_event(record) == []
    assert is_valid(record) is True


def test_required_fields_与emit形状一致():
    assert REQUIRED_FIELDS == ("type", "data", "ts")


@pytest.mark.parametrize("record", [
    "不是 dict",
    {"data": {}, "ts": 1.0},
    {"type": 1, "data": {}, "ts": 1.0},
    {"type": "run_start", "ts": 1.0},
    {"type": "run_start", "data": [], "ts": 1.0},
    {"type": "run_start", "data": {}},
    {"type": "run_start", "data": {}, "ts": "刚刚"},
])
def test_validate_event_非法记录返回非空(record):
    problems = validate_event(record)
    assert problems
    assert is_valid(record) is False


def test_validate_event_问题定位到具体字段():
    assert any("type" in p for p in validate_event({"data": {}, "ts": 1.0}))
    assert any("data" in p for p in validate_event({"type": "think", "ts": 1.0}))
    assert any("ts" in p for p in validate_event({"type": "think", "data": {}}))


def test_validate_event_未知类型仅给出提示():
    problems = validate_event({"type": "从未登记", "data": {}, "ts": 1.0})
    assert any("未知事件类型" in p for p in problems)
    assert len(problems) == 1


# ---- to_sse ----

def test_to_sse_正常格式():
    text = to_sse({"type": "tool_call", "data": {"tool": "file_ops"},
                   "ts": 1.0})
    assert text.startswith("event: tool_call\n")
    assert text.endswith("\n\n")
    payload = text.split("\ndata: ", 1)[1].rstrip("\n")
    assert json.loads(payload) == {"tool": "file_ops"}


def test_to_sse_ping特例():
    assert to_sse({"type": "ping", "data": {}, "ts": 1.0}) == ": ping\n\n"
    assert to_sse({"type": "tool_call"}, event_name="ping") == ": ping\n\n"


def test_to_sse_event_name覆盖事件名():
    text = to_sse({"type": "think", "data": {"x": 1}, "ts": 1.0},
                  event_name="自定义")
    assert text.startswith("event: 自定义\n")
    assert text.endswith("\n\n")


def test_to_sse_不可序列化数据回退str():
    text = to_sse({"type": "think", "data": {"obj": object()}, "ts": 1.0})
    assert text.startswith("event: think\n")
    assert text.endswith("\n\n")


def test_to_sse_非法输入不抛异常():
    assert to_sse(None).startswith("event: unknown\n")


# ---- describe ----

def test_describe_输出单行摘要():
    text = describe({"type": "tool_call", "ts": 1.0,
                     "data": {"task_id": "t0", "tool": "file_ops"}})
    assert text
    assert "\n" not in text
    assert text.startswith("tool_call(")
    assert "task_id=t0" in text
    assert "tool=file_ops" in text


def test_describe_无data回退类型名():
    assert describe({"type": "run_start", "data": {}, "ts": 1.0}) == "run_start"


def test_describe_未知与异常输入也能输出():
    unknown = describe({"type": "从未登记", "data": {"a": 1}, "ts": 1.0})
    assert "从未登记" in unknown
    assert "\n" not in unknown

    multiline = describe({"type": "think", "ts": 1.0,
                          "data": {"msg": "第一行\n第二行"}})
    assert "\n" not in multiline
    assert describe(None) == "unknown"


# ---- known_types ----

def test_known_types_覆盖全部登记类型且已排序():
    types = known_types()
    assert types == sorted(types)
    assert len(types) == len(set(types))

    expected = ["run_start", "plan_created", "run_done", "run_error",
                "interrupt", "priority_changed", "task_done", "task_failed",
                "task_retry", "task_skipped", "task_resumed",
                "task_preempted", "task_interrupted", "think", "tool_call",
                "budget_warning", "budget_exhausted",
                "phase_barrier_task_start", "phase_barrier_summary",
                "dry_run_plan", "session_state"]
    for name in expected:
        assert name in types
        assert name in EVENT_TYPES
