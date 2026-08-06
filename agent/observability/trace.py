"""分布式追踪 —— OTel 风格 Span/Tracer，导出本地 JSON（可后续接 Jaeger）。

- Span: {trace_id, span_id, parent_span_id, name, kind, start/end, duration_ms,
  attributes, status, error}，支持 run/task/tool/llm 等层级调用链；
- Tracer: 线程安全，维护当前 span 上下文（栈），export() 写 JSONL 到 trace_dir。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha-swe.obs.trace")


@dataclass
class Span:
    """一次带调用关系的操作区间。"""
    name: str
    kind: str
    trace_id: str
    span_id: str
    parent_id: Optional[str]
    start_ts: float
    end_ts: Optional[float] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    status: str = "ok"          # ok | error
    error: str = ""

    @property
    def duration_ms(self) -> float:
        end = self.end_ts if self.end_ts is not None else time.time()
        return (end - self.start_ts) * 1000.0

    def end(self, status: str = "ok", error: str = "", **attrs: Any) -> None:
        self.end_ts = time.time()
        self.status = status if status in ("ok", "error") else "ok"
        self.error = error
        if attrs:
            self.attributes.update(attrs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "start_time": self.start_ts,
            "end_time": self.end_ts,
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status,
            "error": self.error,
            "attributes": self.attributes,
        }


class Tracer:
    """span 生命周期与导出；enabled=False 时零成本跳过。"""

    def __init__(self, trace_dir: Optional[str] = "./logs/traces",
                 enabled: bool = True, decision_logger=None):
        self.trace_dir = Path(trace_dir) if trace_dir else None
        self.enabled = enabled
        self._decision = decision_logger
        self._spans: List[Span] = []
        self._stack: List[Span] = []
        self._trace_id = uuid.uuid4().hex[:16]
        self._lock = threading.Lock()

    def start_span(self, name: str, kind: str = "task",
                   parent: Optional[Span] = None,
                   **attrs: Any) -> Span:
        if not self.enabled:
            return Span(name, kind, self._trace_id, "", None, time.time())
        parent_span = parent or self._current()
        span = Span(
            name=name, kind=kind, trace_id=self._trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_id=parent_span.span_id if parent_span else None,
            start_ts=time.time(), attributes=dict(attrs),
        )
        with self._lock:
            self._spans.append(span)
            self._stack.append(span)
        return span

    def end_span(self, span: Span, status: str = "ok",
                 error: str = "", **attrs: Any) -> None:
        if not self.enabled:
            return
        span.end(status, error, **attrs)
        with self._lock:
            if self._stack and self._stack[-1] is span:
                self._stack.pop()

    def _current(self) -> Optional[Span]:
        with self._lock:
            return self._stack[-1] if self._stack else None

    @property
    def current(self) -> Optional[Span]:
        return self._current()

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in self._spans]

    def export(self) -> int:
        """写入 trace_dir/trace_<ts>.jsonl；返回导出条数。"""
        if not self.enabled or self.trace_dir is None:
            return 0
        rows = self.snapshot()
        if not rows:
            return 0
        try:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            path = self.trace_dir / f"trace_{int(time.time())}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if self._decision is not None:
                self._decision.record(
                    "trace.export", "agent.trace_dir", str(path),
                    f"导出 {len(rows)} 个 span 到 {path.name}",
                )
            logger.info("trace 导出 %d spans -> %s", len(rows), path)
            return len(rows)
        except OSError as e:
            logger.warning("trace 导出失败: %s", e)
            return 0

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()
            self._stack.clear()


__all__ = ["Span", "Tracer"]
