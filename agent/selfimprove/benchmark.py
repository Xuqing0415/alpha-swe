# -*- coding: utf-8 -*-
"""主线三 3.3：基准集自动更新。

- 任务完成后评估「代表性」（真实文件改动 / 新增能力维度覆盖 / 难度 / 失败模式暴露），
  代表性高的任务自动提取为基准集条目（status=pending，待用户确认）；
- 台账版本化存储（~/.swe-agent/benchmark_store.json），可回滚/对比；
- trend()：把当前能力画像与基线对比，能力分数连续下降时给出告警。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha-swe.selfimprove.benchmark")

_STATUS_PENDING = "pending"
_STATUS_CONFIRMED = "confirmed"
_STATUS_REJECTED = "rejected"

_WRITE_ACTIONS = ("write", "edit", "append")
_DECLINE_GAP = 0.15  # 当前分数低于基线超过该差距视为下降


def _hash(prompt: str) -> str:
    return hashlib.sha1(str(prompt).strip().encode("utf-8")).hexdigest()[:16]


def _has_real_change(events: List[Dict[str, Any]]) -> bool:
    for e in events or []:
        if e.get("type") != "tool_call":
            continue
        data = e.get("data") or {}
        if not data.get("success"):
            continue
        if data.get("tool") == "file_ops":
            params = data.get("params") or {}
            if str(params.get("action", "")) in _WRITE_ACTIONS:
                return True
    return False


class BenchmarkExtractor:
    """基准集条目提取器：代表性评估 + 版本化台账 + 趋势告警。"""

    def __init__(self, path: Optional[str] = None, enabled: bool = True,
                 profile=None, threshold: float = 0.6) -> None:
        self.enabled = enabled
        self.path = Path(path).expanduser() if path else None
        self.profile = profile
        self.threshold = float(threshold)
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "version": 1, "seq": 0, "entries": [], "baseline": {},
        }
        if self.path is None or not self.enabled:
            return base
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data
        except (OSError, ValueError):
            pass
        return base

    def _save(self) -> None:
        if self.path is None or not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as e:
            logger.warning("基准集台账落盘失败: %s", e)

    # ---- 代表性评估 ----
    def evaluate(self, prompt: str, result) -> Optional[Dict[str, Any]]:
        """评估任务代表性；达到阈值且不与已有条目重复则登记为待确认。"""
        if not self.enabled or result is None:
            return None
        events = list(getattr(result, "events", None) or [])
        tasks = list(getattr(result, "tasks", None) or [])
        phase = str(getattr(result, "phase", "") or "")
        ok = phase in ("completed", "COMPLETED", "ok")
        score, reason = self._representative_score(
            prompt, events, tasks, ok=ok)
        key = _hash(prompt)
        entries = self._data["entries"]
        dup = next((e for e in entries if e["key"] == key), None)
        if dup:
            dup["seen_count"] = int(dup.get("seen_count", 0)) + 1
            dup["last_seen"] = time.time()
            self._save()
            return {"id": dup["id"], "score": score, "reason": reason,
                    "duplicate": True}
        if score < self.threshold:
            return None
        self._data["seq"] += 1
        entry = {
            "id": f"bench_{self._data['seq']}",
            "key": key,
            "instruction": str(prompt)[:500],
            "status": _STATUS_PENDING,
            "score": round(score, 2),
            "difficulty": self._difficulty(len(tasks)),
            "dimensions": self._dimensions(prompt),
            "reason": reason,
            "created_at": time.time(),
            "seen_count": 1,
        }
        entries.append(entry)
        self._save()
        return {"id": entry["id"], "score": score, "reason": reason,
                "duplicate": False}

    def _representative_score(self, prompt: str, events, tasks,
                              ok: bool) -> tuple[float, str]:
        parts: List[str] = []
        score = 0.0
        if _has_real_change(events):
            score += 0.35
            parts.append("真实文件改动")
        dims = self._dimensions(prompt)
        covered = set()
        for e in self._data["entries"]:
            covered.update(e.get("dimensions") or [])
        new_dims = [d for d in dims if d not in covered]
        if new_dims:
            score += 0.25
            parts.append(f"新维度覆盖({','.join(new_dims)})")
        if len(tasks) >= 2:
            score += 0.2
            parts.append(f"多子任务({len(tasks)})")
        if not ok:
            score += 0.2
            parts.append("暴露失败模式")
        reason = " + ".join(parts) if parts else "代表性不足"
        return min(score, 1.0), reason

    @staticmethod
    def _difficulty(subtask_count: int) -> str:
        if subtask_count >= 3:
            return "L4"
        if subtask_count == 2:
            return "L3"
        return "L2"

    def _dimensions(self, prompt: str) -> List[str]:
        from agent.selfimprove.capability import _dimensions_for

        return _dimensions_for(prompt)

    # ---- 用户确认 / 否决 ----
    def confirm(self, entry_id: str) -> bool:
        e = next((x for x in self._data["entries"] if x["id"] == entry_id),
                 None)
        if e is None:
            return False
        e["status"] = _STATUS_CONFIRMED
        e["confirmed_at"] = time.time()
        self._save()
        return True

    def reject(self, entry_id: str) -> bool:
        e = next((x for x in self._data["entries"] if x["id"] == entry_id),
                 None)
        if e is None:
            return False
        e["status"] = _STATUS_REJECTED
        e["rejected_at"] = time.time()
        self._save()
        return True

    def entries(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        entries = list(self._data["entries"])
        if status:
            entries = [e for e in entries if e.get("status") == status]
        return entries

    # ---- 趋势 ----
    def update_baseline(self) -> None:
        """把当前能力画像快照为基线（供下次 run 对比）。"""
        if self.profile is None:
            return
        self._data["baseline"] = self.profile.summary()
        self._data["baseline_at"] = time.time()
        self._save()

    def trend_warnings(self) -> List[str]:
        """当前画像 vs 基线：分数下降的维度给出告警。"""
        if self.profile is None:
            return []
        baseline = self._data.get("baseline") or {}
        warns: List[str] = []
        for dim, base in baseline.items():
            label = base.get("label", dim)
            base_score = float(base.get("score", 0.0) or 0.0)
            if base_score < 0.3:
                continue
            cur = self.profile.score(dim)
            if cur < base_score - _DECLINE_GAP:
                warns.append(
                    f"{label} 能力下降（{base_score:.0%} -> {cur:.0%}），"
                    f"建议回顾该维度近期的失败案例")
        return warns

    def close(self) -> None:
        self._save()
