"""第 9 节测试：Web 观测面板 HTTP API、HTML 与 SSE。"""
import json
import urllib.request

from agent.core.decision_logger import DecisionLogger
from agent.observability import MetricsRegistry, SessionArchive, Tracer
from agent.observability.web import ObservabilityHub, ObservabilityServer


class _Phase:
    value = "running"


class _State:
    phase = _Phase()


class FakeLoop:
    """最小 AgentLoop 桩：供 hub 聚合读取。"""

    def __init__(self):
        self.state = _State()
        self.tracer = Tracer(trace_dir=None, enabled=True)
        self.metrics = MetricsRegistry()
        self.events = [
            {"type": "think", "data": {"content": "分析中"}, "ts": 1.0},
            {"type": "tool_call", "data": {"tool": "ls"}, "ts": 2.0},
        ]
        self._decision = DecisionLogger(enabled=True)
        self._decision.record("parser_strictness", "llm.temperature", 0.2,
                              "解析器模式: strict")
        self._max_rounds = 10
        self.subscribers = []
        self.metrics.set("phase", "running")
        self.metrics.inc("llm_calls")

    def subscribe(self, cb):
        self.subscribers.append(cb)


def _start_server(loop, ws_tmp, prompt="测试任务"):
    (ws_tmp / "sessions").mkdir(parents=True, exist_ok=True)
    SessionArchive(str(ws_tmp / "sessions")).write(
        prompt, loop.events, [], [], result=None)
    hub = ObservabilityHub(loop_provider=lambda: loop,
                           archive_dir=str(ws_tmp / "sessions"),
                           prompt=prompt, session_id="abc123")
    srv = ObservabilityServer(hub, port=0)
    port = srv.start()
    return srv, hub, f"http://127.0.0.1:{port}"


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def test_web_api_status_metrics_spans(ws_tmp):
    loop = FakeLoop()
    root = loop.tracer.start_span("run", "run", prompt="p")
    child = loop.tracer.start_span("llm", "llm")
    loop.tracer.end_span(child, status="ok")
    loop.tracer.end_span(root, status="ok")
    srv, hub, base = _start_server(loop, ws_tmp)
    try:
        code, ctype, body = _get(base + "/api/status")
        assert code == 200 and "json" in ctype
        status = json.loads(body)
        assert status["phase"] == "running" and status["running"] is True
        assert status["prompt"] == "测试任务"
        assert status["session_id"] == "abc123"

        code, _, body = _get(base + "/api/metrics")
        data = json.loads(body)
        assert data["metrics"]["derived"]["llm_calls"] == 1

        code, _, body = _get(base + "/api/spans")
        spans = json.loads(body)
        assert len(spans["roots"]) == 1
        assert spans["roots"][0]["name"] == "run"
        assert len(spans["roots"][0]["children"]) == 1

        code, _, body = _get(base + "/api/decisions")
        dec = json.loads(body)
        assert dec["records"] and "llm.temperature" in dec["summary"]
    finally:
        srv.stop()


def test_web_api_full_and_sessions(ws_tmp):
    loop = FakeLoop()
    srv, hub, base = _start_server(loop, ws_tmp)
    try:
        code, _, body = _get(base + "/api/full")
        full = json.loads(body)
        assert full["status"]["phase"] == "running"
        assert full["events"]
        assert full["sessions"], "档案目录应有会话文件"

        code, _, body = _get(base + "/api/sessions")
        sessions = json.loads(body)["sessions"]
        name = sessions[0]["name"]
        code, _, body = _get(base + "/api/sessions/" + name)
        doc = json.loads(body)
        assert doc["schema"] == "alpha-swe-session-v1"
    finally:
        srv.stop()


def test_web_html_panel(ws_tmp):
    loop = FakeLoop()
    srv, hub, base = _start_server(loop, ws_tmp)
    try:
        code, ctype, body = _get(base + "/")
        assert code == 200 and "text/html" in ctype
        html = body.decode("utf-8")
        assert "Alpha-SWE 观测面板" in html
        assert "EventSource" in html
    finally:
        srv.stop()


def test_web_sse_stream(ws_tmp):
    loop = FakeLoop()
    srv, hub, base = _start_server(loop, ws_tmp)
    try:
        req = urllib.request.Request(base + "/events",
                                     headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.headers.get("Content-Type", "").startswith(
                "text/event-stream")
            first = r.readline().decode("utf-8")
            assert first.startswith("event: snapshot")
    finally:
        srv.stop()


def test_web_session_path_traversal_blocked(ws_tmp):
    loop = FakeLoop()
    srv, hub, base = _start_server(loop, ws_tmp)
    try:
        code, _, body = _get(base + "/api/sessions/..%2F..%2Fetc%2Fpasswd")
        assert code == 200
        assert "error" in json.loads(body)
    finally:
        srv.stop()
