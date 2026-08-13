"""第 10 节测试：OTLP/HTTP JSON 导出 + 结构化 JSONL 日志。"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.observability.otel import JsonLinesLogHandler, OtlpExporter
from agent.observability.trace import Tracer


class _FakeCollector(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length)
        _FakeCollector.received.append((self.path, json.loads(body)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_collector():
    _FakeCollector.received.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCollector)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _two_spans(tracer):
    root = tracer.start_span("run", "run", prompt="p")
    child = tracer.start_span("llm", "llm", model="gpt-4o")
    tracer.end_span(child, status="ok", chars=10)
    tracer.end_span(root, status="ok")
    return tracer.snapshot()


def test_otlp_export_posts_valid_payload(fake_collector):
    tracer = Tracer(trace_dir=None, enabled=True)
    exporter = OtlpExporter(endpoint=fake_collector, enabled=True,
                            service_name="test-svc")
    spans = _two_spans(tracer)
    assert exporter.export(spans) == 2
    assert len(_FakeCollector.received) == 1
    path, payload = _FakeCollector.received[0]
    assert path == "/v1/traces"
    rss = payload["resourceSpans"]
    assert len(rss) == 1
    attrs = {a["key"]: a["value"] for a in rss[0]["resource"]["attributes"]}
    assert attrs["service.name"]["stringValue"] == "test-svc"
    otlp_spans = rss[0]["scopeSpans"][0]["spans"]
    assert len(otlp_spans) == 2
    by_name = {s["name"]: s for s in otlp_spans}
    assert len(by_name["run"]["traceId"]) == 32
    assert len(by_name["run"]["spanId"]) == 16
    assert by_name["llm"]["parentSpanId"] == by_name["run"]["spanId"]
    assert by_name["run"]["status"]["code"] == 1  # StatusCode.OK
    assert by_name["run"]["startTimeUnixNano"].isdigit()
    assert by_name["run"]["endTimeUnixNano"].isdigit()
    llm_attrs = {a["key"]: a["value"] for a in by_name["llm"]["attributes"]}
    assert llm_attrs["chars"]["intValue"] == "10"  # int64 -> 字符串
    assert llm_attrs["model"]["stringValue"] == "gpt-4o"


def test_otlp_export_error_status_and_failure_silent():
    tracer = Tracer(trace_dir=None, enabled=True)
    s = tracer.start_span("tool:ls", "tool")
    tracer.end_span(s, status="error", error="boom")
    exporter = OtlpExporter(endpoint="http://127.0.0.1:1", enabled=True)
    # 端点不可达：静默失败返回 0 且记录 last_error，不抛异常
    assert exporter.export(tracer.snapshot()) == 0
    assert exporter.last_error


def test_otlp_export_disabled_noop():
    tracer = Tracer(trace_dir=None, enabled=True)
    _two_spans(tracer)
    exporter = OtlpExporter(endpoint="http://127.0.0.1:1", enabled=False)
    assert exporter.export(tracer.snapshot()) == 0
    assert not exporter.last_error


def test_tracer_export_invokes_otlp(ws_tmp):
    calls = []

    class FakeExporter:
        def export(self, spans):
            calls.append(list(spans))
            return len(spans)

    tracer = Tracer(trace_dir=str(ws_tmp / "traces"), enabled=True,
                    otlp_exporter=FakeExporter())
    _two_spans(tracer)
    assert tracer.export() == 2  # 本地 JSONL 条数语义不变
    assert len(calls) == 1 and len(calls[0]) == 2


def test_tracer_builds_otlp_exporter_from_config():
    tracer = Tracer(trace_dir=None, enabled=True, otlp_endpoint="http://x:4318",
                    otlp_enabled=True, service_name="svc")
    assert tracer._otlp is not None
    assert tracer._otlp.endpoint == "http://x:4318"
    assert tracer._otlp.service_name == "svc"


def test_json_log_handler_writes_jsonl(ws_tmp):
    handler = JsonLinesLogHandler(str(ws_tmp / "jsonl"), session_id="sess1")
    rec = logging.LogRecord("alpha-swe.x", logging.INFO, "f.py", 1,
                            "hello {v}", (), {"v": 1})
    handler.emit(rec)
    files = list((ws_tmp / "jsonl").glob("*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert row["level"] == "INFO"
    assert row["session"] == "sess1"
    assert "hello" in row["message"]
