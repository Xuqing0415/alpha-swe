# -*- coding: utf-8 -*-
"""主线三 3.2：失败驱动的改进循环。

每次失败 -> 归因 -> 登记/刷新「改进提议」（pending 待验证队列）；
后续相似场景（关键词命中）自动应用该提议：任务成功 -> verified_successes++，
连续 N 次（默认 3）晋升为「自学策略」；应用 M 次（默认 5）仍未达标则丢弃。
提议可被用户否决（reject）；晋升/丢弃均记录决策日志。

持久化：~/.swe-agent/proposals.json
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha-swe.selfimprove.proposals")

STATUS_PENDING = "pending"
STATUS_PROMOTED = "promoted"
STATUS_REJECTED = "rejected"

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")


def _keywords(instruction: str, limit: int = 16) -> List[str]:
    """从指令抽取中英文关键词，作为相似场景的触发词。

    中文用 2-4 字滑窗 shingle（"修复空指针崩溃" -> "空指针"等），
    避免整段连续中文成为一个无法跨句匹配的 token。
    """
    text = str(instruction or "").lower()
    toks: List[str] = []
    for run in _CJK_RE.findall(text):
        n = len(run)
        if n <= 4:
            toks.append(run)
        else:
            for size in (4, 3, 2):
                toks.extend(run[i:i + size] for i in range(0, n - size + 1))
    toks.extend(_ASCII_RE.findall(text))
    seen: List[str] = []
    for t in toks:
        if t not in seen:
            seen.append(t)
    return seen[:limit]


def _key(category: str, action: str) -> str:
    return hashlib.sha1(f"{category}:{action}".encode("utf-8")).hexdigest()[:12]


class ProposalStore:
    """改进提议队列：登记 / 匹配 / 验证 / 晋升 / 否决。"""

    def __init__(self, path: Optional[str] = None, enabled: bool = True,
                 promote_threshold: int = 3, reject_after: int = 5) -> None:
        self.enabled = enabled
        self.path = Path(path).expanduser() if path else None
        self.promote_threshold = max(1, int(promote_threshold))
        self.reject_after = max(1, int(reject_after))
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        base: Dict[str, Any] = {"version": 1, "seq": 0, "proposals": {}}
        if self.path is None or not self.enabled:
            return base
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("proposals"), dict):
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
            logger.warning("改进提议台账落盘失败: %s", e)

    # ---- 登记 ----
    def create_or_bump(self, category: str, instruction: str,
                       action: str = "") -> str:
        """失败后登记提议；同类别同措施的提议已存在则刷新触发词与 last_seen。"""
        action = (action or "").strip()
        pid = _key(category, action)
        proposals = self._data["proposals"]
        now = time.time()
        if pid in proposals:
            p = proposals[pid]
            p["last_seen"] = now
            p["seen_count"] = int(p.get("seen_count", 0)) + 1
            merged = list(dict.fromkeys(p.get("trigger_keywords", [])
                                        + _keywords(instruction)))
            p["trigger_keywords"] = merged[:12]
            self._save()
            return pid
        self._data["seq"] += 1
        proposals[pid] = {
            "id": pid,
            "seq": self._data["seq"],
            "category": category,
            "action": action,
            "status": STATUS_PENDING,
            "trigger_keywords": _keywords(instruction),
            "verified_successes": 0,
            "applied_count": 0,
            "seen_count": 1,
            "created_at": now,
            "last_seen": now,
        }
        self._save()
        return pid

    # ---- 匹配 ----
    def match(self, instruction: str) -> List[str]:
        """返回与当前指令相似的待验证提议 id（作为本轮应用目标）。"""
        if not self.enabled:
            return []
        text = str(instruction or "").lower()
        ids: List[str] = []
        for pid, p in self._data["proposals"].items():
            if p.get("status") != STATUS_PENDING:
                continue
            kws = p.get("trigger_keywords") or []
            if any(k in text for k in kws):
                ids.append(pid)
        return ids

    # ---- 验证 ----
    def verify(self, pid: str, ok: bool) -> str:
        """应用目标提议后验证：成功计数，达到阈值晋升；应用过多未达标则丢弃。"""
        p = self._data["proposals"].get(pid)
        if p is None or p.get("status") != STATUS_PENDING:
            return str(p.get("status", "")) if p else ""
        p["applied_count"] = int(p.get("applied_count", 0)) + 1
        if ok:
            p["verified_successes"] = int(p.get("verified_successes", 0)) + 1
            if p["verified_successes"] >= self.promote_threshold:
                p["status"] = STATUS_PROMOTED
                p["promoted_at"] = time.time()
                self._save()
                return STATUS_PROMOTED
        elif int(p.get("applied_count", 0)) >= self.reject_after:
            p["status"] = STATUS_REJECTED
            p["rejected_at"] = time.time()
            self._save()
            return STATUS_REJECTED
        self._save()
        return p["status"]

    # ---- 用户否决 / 手动晋升 ----
    def reject(self, pid: str) -> bool:
        p = self._data["proposals"].get(pid)
        if p is None:
            return False
        p["status"] = STATUS_REJECTED
        p["rejected_at"] = time.time()
        self._save()
        return True

    def promote(self, pid: str) -> bool:
        """手动晋升（评估工具/用户确认用）。"""
        p = self._data["proposals"].get(pid)
        if p is None:
            return False
        p["status"] = STATUS_PROMOTED
        p["promoted_at"] = time.time()
        self._save()
        return True

    # ---- 读取 ----
    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        proposals = list(self._data["proposals"].values())
        if status:
            proposals = [p for p in proposals if p.get("status") == status]
        return sorted(proposals, key=lambda p: p.get("seq", 0))

    def summary(self) -> Dict[str, int]:
        counts = {s: 0 for s in (STATUS_PENDING, STATUS_PROMOTED,
                                 STATUS_REJECTED)}
        for p in self._data["proposals"].values():
            s = p.get("status")
            if s in counts:
                counts[s] += 1
        return counts

    def close(self) -> None:
        self._save()
