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


def files_modified_from_events(events: List[Dict[str, Any]]) -> List[str]:
    """从事件流提取被写/编辑/追加/删除的文件路径（去重保序）。

    与 CLI 输出的 files_modified 同源，供会话档案分析与复盘使用。
    """
    seen: set = set()
    out: List[str] = []
    for ev in events:
        if ev.get("type") != "tool_call":
            continue
        data = ev.get("data") or {}
        if not data.get("success"):
            continue
        if data.get("tool") != "file_ops":
            continue
        params = data.get("params") or {}
        action = params.get("action")
        if action not in ("write", "edit", "append", "delete", "rm"):
            continue
        path = params.get("path")
        if path and path not in seen:
            seen.add(path)
            out.append(str(path))
    return out


def summarize_session(doc: Dict[str, Any]) -> Dict[str, Any]:
    """生成长任务会话复盘摘要（阶段二 2.3 事后分析）。

    输入为 SessionArchive 写入的会话档案（schema: alpha-swe-session-v1），
    输出覆盖：完成状态、轮次/token、工具调用成功率、重试、压缩触发时机
    （是否过早）、任务进度、修改文件与错误清单。
    """
    result = doc.get("result") or {}
    metrics = doc.get("metrics") or {}
    counters = metrics.get("counters", {}) or {}
    gauges = metrics.get("gauges", {}) or {}
    events = doc.get("events", []) or []
    decisions = doc.get("decisions", []) or []

    tool_events = [e for e in events
                   if e.get("type") == "tool_call" and e.get("data")]
    tool_ok = [e for e in tool_events if e["data"].get("success")]
    tool_fail = [e for e in tool_events if not e["data"].get("success")]

    # 首次实际压缩发生的时机：按时间戳找第一条 compression_level 决策，
    # 统计其之前已发生的 think/tool_call 决策点数量（>0 说明不是开局即压）。
    compression_decisions = [
        d for d in decisions if d.get("name") == "compression_level"
    ]
    compression_total = int(counters.get("compressions", 0) or 0)
    compression_first_after_events = 0
    if compression_decisions:
        first_ts = min(
            float(d.get("timestamp", 0.0)) for d in compression_decisions)
        decision_points = [
            e for e in events
            if e.get("type") in ("think", "tool_call", "task_start")
        ]
        compression_first_after_events = sum(
            1 for e in decision_points if float(e.get("ts", 0.0)) < first_ts)

    tool_calls = int(counters.get("tool_calls", 0) or 0)
    failures = int(counters.get("tool_failures", 0) or 0)
    errors = [
        str(e["data"].get("output", ""))[:200]
        for e in tool_fail if e["data"].get("output")
    ]
    if not errors and not result.get("ok"):
        errors = [str(result.get("final_answer", "") or "")[:200]]

    return {
        "session_id": doc.get("session_id", ""),
        "ok": bool(result.get("ok", False)),
        "phase": str(result.get("phase", "")),
        "final_answer": str(result.get("final_answer", "")),
        "rounds": int(gauges.get("rounds", 0)
                      or result.get("total_rounds", 0) or 0),
        "llm_calls": int(counters.get("llm_calls", 0) or 0),
        "tokens": int(counters.get("token_usage", 0) or 0),
        "tool_calls": tool_calls,
        "tool_failures": failures,
        "tool_success_rate": round(
            (tool_calls - failures) / tool_calls, 3) if tool_calls else None,
        "retries": int(counters.get("retries", 0) or 0),
        "compressions": compression_total,
        "compression_first_after_events": compression_first_after_events,
        "tasks": {
            "total": int(gauges.get("tasks_total", 0) or 0),
            "completed": int(gauges.get("tasks_completed", 0) or 0),
            "failed": int(gauges.get("tasks_failed", 0) or 0),
            "skipped": int(gauges.get("tasks_skipped", 0) or 0),
        },
        "files_modified": files_modified_from_events(events),
        "decisions": len(decisions),
        "events": len(events),
        "spans": len(doc.get("spans", []) or []),
        "errors": errors,
    }


__all__ = [
    "SessionArchive", "SessionReplay",
    "files_modified_from_events", "summarize_session",
]

