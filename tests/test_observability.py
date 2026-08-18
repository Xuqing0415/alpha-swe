"""阶段七可观测性测试：Tracer / MetricsRegistry / SessionArchive+Replay。"""
import json


from agent.observability import (MetricsRegistry, SessionArchive, SessionReplay,
                                 Tracer)


# ---- Tracer：嵌套 span / 父子关联 / 导出 ----
def test_tracer_nested_spans_and_snapshot():
    tracer = Tracer(trace_dir=None, enabled=True)
    root = tracer.start_span("run", "run", prompt="p")
    child = tracer.start_span("llm", "llm")
    tracer.end_span(child, status="ok", chars=10)
    tracer.end_span(root, status="ok", total_rounds=1)
    spans = tracer.snapshot()
    assert len(spans) == 2
    by_name = {s["name"]: s for s in spans}
    assert by_name["llm"]["parent_span_id"] == by_name["run"]["span_id"]
    assert by_name["run"]["status"] == "ok"
    assert by_name["run"]["duration_ms"] >= 0
    assert by_name["llm"]["attributes"]["chars"] == 10
    assert by_name["run"]["attributes"]["total_rounds"] == 1


def test_tracer_export_writes_jsonl(ws_tmp):
    tracer = Tracer(trace_dir=str(ws_tmp / "traces"), enabled=True)
    s = tracer.start_span("run", "run")
    tracer.end_span(s)
    n = tracer.export()
    assert n == 1
    files = list((ws_tmp / "traces").glob("*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert row["name"] == "run"
    assert row["status"] == "ok"


def test_tracer_disabled_is_zero_cost(ws_tmp):
    tracer = Tracer(trace_dir=str(ws_tmp / "traces"), enabled=False)
    s = tracer.start_span("run", "run")
    tracer.end_span(s)
    assert tracer.export() == 0
    assert tracer.snapshot() == []


# ---- MetricsRegistry：计数 / 派生指标 / 告警 ----
def test_metrics_counters_and_derived():
    m = MetricsRegistry(enabled=True)
    m.inc("llm_calls")
    m.record_token_usage(100)
    m.record_tool_result(True)
    m.record_tool_result(False)
    m.set("phase", "running")
    snap = m.snapshot()
    d = snap["derived"]
    assert d["llm_calls"] == 1
    assert d["token_total"] == 100
    assert d["tool_calls"] == 2
    assert d["tool_success_rate"] == 0.5
    assert d["consecutive_failures"] == 1
    assert d["phase"] == "running"
    assert snap["counters"]["token_usage"] == 100.0


def test_metrics_alerts_thresholds():
    # 轮次逼近上限
    m = MetricsRegistry(enabled=True)
    m.set("rounds", 9)
    alerts = m.alerts(max_rounds=10, token_rate=1e9, consecutive_failures=99)
    assert any("轮次" in a for a in alerts)
    # 连续失败
    m2 = MetricsRegistry(enabled=True)
    m2.record_tool_result(False)
    m2.record_tool_result(False)
    m2.record_tool_result(False)
    a2 = m2.alerts(token_rate=1e9, max_rounds=100)
    assert any("连续失败" in a for a in a2)
    # token 速率
    m3 = MetricsRegistry(enabled=True)
    m3.record_token_usage(10_000)
    a3 = m3.alerts(token_rate=100.0, max_rounds=100)
    assert any("token" in a for a in a3)


def test_metrics_disabled_noop():
    m = MetricsRegistry(enabled=False)
    m.inc("llm_calls")
    m.record_tool_result(False)
    snap = m.snapshot()
    assert snap["counters"] == {}
    assert snap["derived"]["tool_calls"] == 0


# ---- SessionArchive / SessionReplay：档案打包与时间线回放 ----
def test_archive_write_load_replay(ws_tmp):
    arch = SessionArchive(str(ws_tmp / "sessions"), enabled=True)
    events = [
        {"type": "run_start", "data": {}, "ts": 1.0},
        {"type": "tool_call", "data": {"tool": "ls"}, "ts": 2.0},
    ]
    spans = [{"name": "run", "kind": "run", "start_time": 0.5}]
    decisions = [{"name": "max_loop_iterations", "timestamp": 1.5}]
    path = arch.write("prompt", events, spans, decisions,
                      {"counters": {"a": 1}}, result=None)
    assert path is not None and path.exists()

    replay = SessionReplay.load(str(path))
    tl = replay.timeline()
    assert len(tl) == 4
    # 按时间戳合并排序：0.5 span -> 1.0 event -> 1.5 decision -> 2.0 event
    kinds = [r["kind"] for r in tl]
    assert kinds == ["span", "event", "decision", "event"]
    assert replay.step(0)["kind"] == "span"
    assert replay.step(0)["label"].startswith("span[")
    assert replay.step(99) == {}
    assert len(replay) == 4
    assert replay.archive["prompt"] == "prompt"
    assert replay.archive["metrics"]["counters"]["a"] == 1


def test_archive_disabled_returns_none(ws_tmp):
    arch = SessionArchive(str(ws_tmp / "sessions"), enabled=False)
    path = arch.write("p", [], [], [])
    assert path is None


def test_tracer_get_timeline_data_relative():
    """get_timeline_data 返回相对起点的简化 span 列表。"""
    from agent.observability.trace import Tracer

    tr = Tracer(trace_dir=None, enabled=True)
    s1 = tr.start_span("task:a", "task")
    s1.end("ok")
    s2 = tr.start_span("tool:ls", "tool")
    s2.end("error")
    rows = tr.get_timeline_data()
    assert len(rows) == 2
    # 相对起点：第一条 start 为 0
    assert rows[0]["start"] == 0.0
    assert rows[0]["name"] == "task:a"
    assert rows[1]["kind"] == "tool"
    assert rows[1]["status"] == "error"
    assert rows[0]["duration"] >= 0
    # 进行中的 span 不出现
    tr.start_span("task:running", "task")
    rows2 = tr.get_timeline_data()
    assert len(rows2) == 2

