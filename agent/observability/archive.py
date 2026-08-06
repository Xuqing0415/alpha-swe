"""会话档案与回放 —— 把事件、span、决策日志、指标打包为单个 JSON，支持时间线回放。

- SessionArchive.build()/write(): logs/sessions/session_<ts>_<id>.json；
- SessionReplay.timeline(): 按时间戳合并 events/spans/decisions，step() 单步回放。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha-swe.obs.archive")


class SessionArchive:
    def __init__(self, archive_dir: Optional[str] = "./logs/sessions",
                 enabled: bool = True):
        self.archive_dir = Path(archive_dir) if archive_dir else None
        self.enabled = enabled

    def build(self, prompt: str, events: List[Dict[str, Any]],
              spans: List[Dict[str, Any]],
              decisions: List[Dict[str, Any]],
              metrics: Optional[Dict[str, Any]] = None,
              result: Optional[Any] = None,
              session_id: str = "") -> Dict[str, Any]:
        return {
            "schema": "alpha-swe-session-v1",
            "session_id": session_id or uuid.uuid4().hex[:12],
            "created_at": time.time(),
            "prompt": prompt,
            "result": {
                "ok": bool(result and getattr(result, "ok", False)),
                "phase": getattr(result, "phase", "").value
                if result and hasattr(result, "phase") else "",
                "final_answer": getattr(result, "final_answer", ""),
                "total_rounds": getattr(result, "total_rounds", 0),
            } if result is not None else None,
            "events": list(events),
            "spans": list(spans),
            "decisions": list(decisions),
            "metrics": metrics or {},
        }

    def write(self, prompt: str, events: List[Dict[str, Any]],
              spans: List[Dict[str, Any]],
              decisions: List[Dict[str, Any]],
              metrics: Optional[Dict[str, Any]] = None,
              result: Optional[Any] = None,
              session_id: str = "") -> Optional[Path]:
        """写档案文件；返回路径（失败或未启用返回 None）。"""
        if not self.enabled or self.archive_dir is None:
            return None
        doc = self.build(prompt, events, spans, decisions, metrics, result,
                         session_id=session_id)
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            path = self.archive_dir / (
                f"session_{int(time.time())}_{doc['session_id']}.json"
            )
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            logger.info("会话档案已写入: %s", path)
            return path
        except OSError as e:
            logger.warning("会话档案写入失败: %s", e)
            return None

    @staticmethod
    def load(path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


class SessionReplay:
    """按时间线逐步回放一个会话档案。"""

    def __init__(self, archive: Dict[str, Any]):
        self.archive = archive
        self._timeline: Optional[List[Dict[str, Any]]] = None

    @classmethod
    def load(cls, path: str) -> "SessionReplay":
        return cls(SessionArchive.load(path))

    def timeline(self) -> List[Dict[str, Any]]:
        if self._timeline is not None:
            return self._timeline
        rows: List[Dict[str, Any]] = []
        for i, e in enumerate(self.archive.get("events", [])):
            rows.append({
                "ts": e.get("ts", 0.0), "kind": "event",
                "label": f"event[{i}] {e.get('type', '')}",
                "payload": e,
            })
        for s in self.archive.get("spans", []):
            rows.append({
                "ts": s.get("start_time", 0.0), "kind": "span",
                "label": f"span[{s.get('kind', '')}] {s.get('name', '')}",
                "payload": s,
            })
        for d in self.archive.get("decisions", []):
            rows.append({
                "ts": d.get("timestamp", 0.0), "kind": "decision",
                "label": f"decision {d.get('name', '')}",
                "payload": d,
            })
        rows.sort(key=lambda r: r["ts"])
        self._timeline = rows
        return rows

    def __len__(self) -> int:
        return len(self.timeline())

    def step(self, index: int) -> Dict[str, Any]:
        """取第 index 条时间线（越界返回空 dict）。"""
        rows = self.timeline()
        if not 0 <= index < len(rows):
            return {}
        return rows[index]


__all__ = ["SessionArchive", "SessionReplay"]
