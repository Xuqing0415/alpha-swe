"""OpenTelemetry/Jaeger 导出（方案第 10 节）——OTLP/HTTP JSON + 结构化 JSONL 日志。

- OtlpExporter：把自研 OTel 风格 Span（agent/observability/trace.py 的
  snapshot dict）映射为 OTLP/HTTP JSON，POST 到 Collector / Jaeger v2 /
  Tempo 的 /v1/traces；失败静默降级并记录决策点，不阻塞主流程；
- JsonLinesLogHandler：logging.Handler，把每条日志写成结构化 JSONL
  （ts/level/logger/session/message），供 Loki/ELK 等离线接入。

纯标准库实现（urllib + json），不依赖 opentelemetry SDK，离线环境也能
直接对接 OTLP Collector；本地 JSONL 导出（Tracer.export）始终作为回退。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha-swe.obs.otel")

OTLP_TRACES_PATH = "/v1/traces"   # OTLP/HTTP 追踪接收路径
DEFAULT_TIMEOUT = 3.0             # 单次导出超时（秒）
SPAN_KIND_INTERNAL = 1            # otel SpanKind.INTERNAL
STATUS_CODE_OK = 1                # otel StatusCode.OK
STATUS_CODE_ERROR = 2             # otel StatusCode.ERROR


def _any_value(value: Any) -> Dict[str, Any]:
    """Python 值 -> OTLP AnyValue（int64 按 proto JSON 规范编码为字符串）。"""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_any_value(v) for v in value]}}
    if isinstance(value, dict):
        return {"kvlistValue": {"values": [
            {"key": str(k), "value": _any_value(v)} for k, v in value.items()]}}
    return {"stringValue": str(value)}


def _kv(key: str, value: Any) -> Dict[str, Any]:
    return {"key": key, "value": _any_value(value)}


def _to_nano(ts: float) -> str:
    return str(int(ts * 1_000_000_000))


def _pad_trace_id(trace_id: str) -> str:
    """自研 trace_id 为 16 位 hex（8 字节），补齐为 OTLP 要求的 32 位 hex。"""
    return (trace_id or "").ljust(32, "0")


def _span_to_otlp(span: Dict[str, Any]) -> Dict[str, Any]:
    """span snapshot dict -> OTLP Span JSON。"""
    status_code = STATUS_CODE_OK if span.get("status") == "ok" else STATUS_CODE_ERROR
    attrs: List[Dict[str, Any]] = []
    for key, value in (span.get("attributes") or {}).items():
        try:
            attrs.append(_kv(str(key), value))
        except Exception:
            attrs.append(_kv(str(key), str(value)))
    return {
        "traceId": _pad_trace_id(span.get("trace_id", "")),
        "spanId": (span.get("span_id") or "").ljust(16, "0"),
        "parentSpanId": span.get("parent_span_id") or "",
        "name": span.get("name", ""),
        "kind": SPAN_KIND_INTERNAL,
        "startTimeUnixNano": _to_nano(float(span.get("start_time") or time.time())),
        "endTimeUnixNano": _to_nano(float(span.get("end_time") or time.time())),
        "attributes": attrs,
        "status": {"code": status_code, "message": span.get("error") or ""},
        "events": [],
    }


class OtlpExporter:
    """把 span dict 列表导出到 OTLP/HTTP 端点；失败静默降级。"""

    def __init__(self, endpoint: str = "", enabled: bool = True,
                 service_name: str = "alpha-swe",
                 timeout: float = DEFAULT_TIMEOUT,
                 decision_logger=None):
        self.endpoint = (endpoint or "").strip().rstrip("/")
        self.enabled = enabled and bool(self.endpoint)
        self.service_name = service_name or "alpha-swe"
        self.timeout = timeout
        self._decision = decision_logger
        self.last_error: str = ""
        self.exported_count: int = 0

    def export(self, spans: List[Dict[str, Any]]) -> int:
        """POST OTLP JSON；返回成功导出的 span 数（失败返回 0，不抛异常）。"""
        if not self.enabled or not spans:
            return 0
        try:
            payload = self._build_payload(spans)
            url = self.endpoint + OTLP_TRACES_PATH
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                code = getattr(resp, "status", 200)
            if code >= 400:
                raise RuntimeError(f"OTLP 端点返回 HTTP {code}")
            self.exported_count += len(spans)
            self.last_error = ""
            if self._decision is not None:
                try:
                    self._decision.record(
                        "otel.export", "agent.otel_endpoint", self.endpoint,
                        f"OTLP 导出 {len(spans)} 个 span -> {url}",
                    )
                except Exception:
                    pass
            logger.info("OTLP 导出 %d spans -> %s", len(spans), url)
            return len(spans)
        except Exception as e:
            self.last_error = str(e)
            logger.warning("OTLP 导出失败: %s", e)
            return 0

    def _build_payload(self, spans: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "resourceSpans": [{
                "resource": {"attributes": [
                    _kv("service.name", self.service_name),
                    _kv("telemetry.sdk.name", "alpha-swe"),
                    _kv("telemetry.sdk.language", "python"),
                    _kv("telemetry.sdk.version", "0.1.0"),
                ]},
                "scopeSpans": [{
                    "scope": {"name": "alpha-swe.tracer",
                              "version": "0.1.0"},
                    "spans": [_span_to_otlp(s) for s in spans],
                }],
            }],
        }


class JsonLinesLogHandler(logging.Handler):
    """结构化 JSONL 日志：每条记录一行 JSON，供外部日志系统接入。"""

    def __init__(self, log_dir: str, session_id: str = "",
                 level: int = logging.DEBUG):
        super().__init__(level)
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self._path = self._dir / f"structured_{stamp}_{os.getpid()}.jsonl"
        self._session = session_id
        self._lock = threading.Lock()
        logger.info("结构化 JSONL 日志: %s", self._path)

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry: Dict[str, Any] = {
                "ts": round(time.time(), 3),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if self._session:
                entry["session"] = self._session
            # 转发常用结构化字段（若记录上存在）
            for key in ("trace_id", "task_id", "tool", "status",
                        "duration_ms", "span_id"):
                value = getattr(record, key, None)
                if value is not None:
                    entry[key] = value
            line = json.dumps(entry, ensure_ascii=False)
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass  # 结构化日志写入失败不影响主流程


__all__ = ["OtlpExporter", "JsonLinesLogHandler", "OTLP_TRACES_PATH"]
