# -*- coding: utf-8 -*-
"""统一事件流 schema 的接线测试：SSE 生成器与 Web 观测面板。"""
import asyncio
import json
import logging
import queue
import urllib.request

import pytest

from agent.core.decision_logger import DecisionLogger
from agent.observability import MetricsRegistry, Tracer
from agent.observability.event_schema import EventKind
from agent.observability.web import (_HTML_PAGE, ObservabilityHub,
                                     ObservabilityServer)
from server.events import sse_generator

SSE_LOGGER = "alpha-swe.server.events"


async def _drain(items, **kwargs):
    """把 items 依次入队并消费到 done 事件为止的全部 SSE 文本块。"""
    queue = asyncio.Queue()
    for item in items:
        queue.put_nowait(item)
    chunks = []
    async for chunk in sse_generator(queue, **kwargs):
        chunks.append(chunk)
    return chunks


# ---- sse_generator：输出格式回归 ----

async def test_sse_普通事件格式与历史实现一致():
    chunks = await _drain([
        {"type": "think", "data": {"content": "分析中"}, "ts": 1.0},
        {"type": "tool_call", "data": {"tool": "ls"}, "ts": 2.0},
        {"type": "done", "data": {}, "ts": 3.0},
    ])
    assert chunks == [
        'event: think\ndata: {"content": "分析中"}\n\n',
        'event: tool_call\ndata: {"tool": "ls"}\n\n',
        "event: done\ndata: {}\n\n",
    ]


async def test_sse_ping特例与done后结束流():
    chunks = await _drain([
        {"type": "ping", "data": {}, "ts": 1.0},
        {"type": "done", "data": {}, "ts": 2.0},
        {"type": "think", "data": {"content": "done 之后不再输出"}, "ts": 3.0},
    ])
    assert chunks[0] == ": ping\n\n"
    assert chunks[1] == "event: done\ndata: {}\n\n"
    assert len(chunks) == 2, "done 事件之后必须结束流"


async def test_sse_缺失type沿用message缺省且补齐data():
    chunks = await _drain([
        {"data": {"x": 1}},
        {"type": "run_done"},
        {"type": "done", "data": {}},
    ])
    assert chunks[0] == 'event: message\ndata: {"x": 1}\n\n'
    assert chunks[1] == "event: run_done\ndata: {}\n\n"
    assert chunks[2] == "event: done\ndata: {}\n\n"


async def test_sse_不可序列化数据回退str():
    chunks = await _drain([
        {"type": "think", "data": {"obj": object()}, "ts": 1.0},
        {"type": "done", "data": {}},
    ])
    assert chunks[0].startswith("event: think\ndata: {")
    assert chunks[0].endswith("}\n\n")


# ---- sse_generator：strict_validation ----

async def test_sse_严格模式校验不通过仍发送并告警(caplog):
    caplog.set_level(logging.WARNING, logger=SSE_LOGGER)
    chunks = await _drain([
        {"type": "从未登记的类型", "data": {}, "ts": 1.0},
        {"data": {}, "ts": 2.0},
        {"type": "done", "data": {}},
    ], strict_validation=True)

    assert len(chunks) == 3, "校验不通过也必须照常发送"
    assert chunks[0].startswith("event: 从未登记的类型\n")
    assert chunks[1].startswith("event: message\n")
    assert chunks[2] == "event: done\ndata: {}\n\n"

    records = [r for r in caplog.records if r.name == SSE_LOGGER]
    assert len(records) == 2, "两个不合法业务事件各一条告警"
    assert all("SSE 事件校验不通过" in r.getMessage() for r in records)
    assert "未知事件类型" in records[0].getMessage()
    assert "缺少字段: type" in records[1].getMessage()


async def test_sse_严格模式跳过ping与done控制帧(caplog):
    caplog.set_level(logging.WARNING, logger=SSE_LOGGER)
    chunks = await _drain([
        {"type": "ping", "data": {}},
        {"type": "done", "data": {}},
    ], strict_validation=True)
    assert chunks == [": ping\n\n", "event: done\ndata: {}\n\n"]
    assert not [r for r in caplog.records if r.name == SSE_LOGGER]


async def test_sse_默认模式不校验不产生日志(caplog):
    caplog.set_level(logging.WARNING, logger=SSE_LOGGER)
    chunks = await _drain([
        {"type": "从未登记的类型", "data": {}, "ts": 1.0},
        {"type": "done", "data": {}},
    ])
    assert len(chunks) == 2
    assert not [r for r in caplog.records if r.name == SSE_LOGGER]


# ---- ObservabilityHub：kind 分组接线 ----

class FakeLoop:
    """最小 loop 桩：emit 模拟 AgentLoop._emit 的写入事件表 + 广播订阅者。"""

    def __init__(self):
        self.tracer = Tracer(trace_dir=None, enabled=True)
        self.metrics = MetricsRegistry()
        self._decision = DecisionLogger(enabled=True)
        self._max_rounds = 10
        self.events = []
        self._subscribers = []

    def subscribe(self, callback):
        self._subscribers.append(callback)

    def emit(self, record):
        self.events.append(record)
        for callback in list(self._subscribers):
            callback(record)


def _hub_of(loop):
    return ObservabilityHub(loop_provider=lambda: loop)


def test_push_归一化并附加kind且不修改入参():
    hub = _hub_of(FakeLoop())
    record = {"type": "tool_call", "data": {"tool": "ls"}}
    hub._push(record)

    queued = hub.wait_event(timeout=0.5)
    assert queued is not None
    assert queued["kind"] == EventKind.TOOL.value == "tool"
    assert isinstance(queued["ts"], float)
    assert queued["data"] == {"tool": "ls"}
    assert record == {"type": "tool_call", "data": {"tool": "ls"}}, \
        "normalize_event 不得修改入参"
    assert "kind" not in record and "ts" not in record


def test_push_后hub_events带kind且不改动原始记录():
    loop = FakeLoop()
    hub = _hub_of(loop)
    hub._ensure_subscribed()
    loop.emit({"type": "think", "data": {"content": "分析中"}, "ts": 1.0})

    events = hub.events()
    assert events and events[0]["kind"] == "thought"
    assert events[0]["type"] == "think"
    assert events[0]["data"] == {"content": "分析中"}
    assert loop.events[0] == {"type": "think", "data": {"content": "分析中"},
                              "ts": 1.0}, "原始事件表不得被附加 kind"
    assert "kind" not in loop.events[0]

    queued = hub.wait_event(timeout=0.5)
    assert queued is not None and queued["kind"] == "thought"


@pytest.mark.parametrize("event_type,expected_kind", [
    ("tool_call", "tool"),
    ("think", "thought"),
    ("run_start", "lifecycle"),
    ("task_done", "task"),
    ("session_state", "state"),
    ("budget_warning", "budget"),
    ("phase_barrier_summary", "barrier"),
    ("从未登记的类型", "other"),
])
def test_push_各类事件映射到对应分组(event_type, expected_kind):
    hub = _hub_of(FakeLoop())
    hub._push({"type": event_type, "data": {}, "ts": 1.0})
    queued = hub.wait_event(timeout=0.5)
    assert queued["kind"] == expected_kind


def test_push_队列满时静默丢弃():
    hub = _hub_of(FakeLoop())
    hub._queue = queue.Queue(maxsize=1)
    hub._push({"type": "think", "data": {}, "ts": 1.0})
    hub._push({"type": "think", "data": {}, "ts": 2.0})
    assert hub.wait_event(timeout=0.5)["ts"] == 1.0
    assert hub.wait_event(timeout=0.05) is None


# ---- Web 面板：分组筛选接线 ----

def test_web页面含kind分组属性与筛选按钮():
    assert 'data-kind="' in _HTML_PAGE
    assert 'id="event-kinds"' in _HTML_PAGE
    assert 'data-kind="tool"' in _HTML_PAGE
    assert "applyKindFilter" in _HTML_PAGE
    for kind in ("全部", "lifecycle", "task", "thought", "tool", "state",
                 "budget", "barrier", "error", "other"):
        assert kind in _HTML_PAGE


def test_web_api与SSE事件均携带kind(ws_tmp):
    loop = FakeLoop()
    (ws_tmp / "sessions").mkdir(parents=True, exist_ok=True)
    hub = ObservabilityHub(loop_provider=lambda: loop,
                           archive_dir=str(ws_tmp / "sessions"),
                           prompt="事件分组", session_id="s-kind")
    srv = ObservabilityServer(hub, port=0)
    port = srv.start()
    base = f"http://127.0.0.1:{port}"
    try:
        loop.emit({"type": "tool_call", "data": {"tool": "ls"}, "ts": 1.0})
        loop.emit({"type": "think", "data": {"content": "分析"}, "ts": 2.0})

        with urllib.request.urlopen(base + "/api/full", timeout=10) as r:
            events = json.loads(r.read())["events"]
        kinds = {e["type"]: e["kind"] for e in events}
        assert kinds == {"tool_call": "tool", "think": "thought"}

        with urllib.request.urlopen(base + "/events", timeout=10) as r:
            assert r.readline().decode("utf-8").startswith("event: snapshot")
            payload = r.readline().decode("utf-8")
        snapshot = json.loads(payload[len("data: "):])
        assert {e["type"]: e["kind"] for e in snapshot["events"]} == kinds
    finally:
        srv.stop()
