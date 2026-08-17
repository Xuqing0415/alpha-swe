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
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("alpha-swe.selfimprove.proposals")

STATUS_PENDING = "pending"
STATUS_PROMOTED = "promoted"
STATUS_REJECTED = "rejected"
STATUS_LOCAL = "local"  # 项目级经验：仅单一场景有效，未达泛化晋升标准

# 3.2A 泛化性测试：晋升前需在“相似（>=0.7）+ 相关（0.4~0.7）”两个场景验证有效
SCENE_SIMILAR = "similar"
SCENE_RELATED = "related"
SCENE_DISTANT = "distant"
_SIMILAR_THRESHOLD = 0.7
_RELATED_THRESHOLD = 0.4

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


def _similarity_tokens(text: str):
    """场景相似度 token：2 字 CJK 滑窗 + ASCII 标识符（粒度适中，抗噪声）。"""
    t = str(text or "").lower()
    toks = set()
    for run in _CJK_RE.findall(t):
        toks.update(run[i:i + 2] for i in range(len(run) - 1))
    toks.update(_ASCII_RE.findall(t))
    return toks


def scene_similarity(origin: str, verify: str) -> float:
    """重叠系数（共享 token / 较小集合），衡量两个场景的相关程度。"""
    a = _similarity_tokens(origin)
    b = _similarity_tokens(verify)
    if not a or not b:
        return 0.0
    shared = len(a & b)
    return shared / min(len(a), len(b))


def scene_bucket(sim: float) -> str:
    if sim >= _SIMILAR_THRESHOLD:
        return SCENE_SIMILAR
    if sim >= _RELATED_THRESHOLD:
        return SCENE_RELATED
    return SCENE_DISTANT


class ProposalStore:
    """改进提议队列：登记 / 匹配 / 验证 / 晋升 / 否决。"""

    def __init__(self, path: Optional[str] = None, enabled: bool = True,
                 promote_threshold: int = 3, reject_after: int = 5,
                 require_generalization: bool = True,
                 conflict_threshold: int = 5,
                 conflict_detector: Optional[Callable] = None) -> None:
        self.enabled = enabled
        self.path = Path(path).expanduser() if path else None
        self.promote_threshold = max(1, int(promote_threshold))
        self.reject_after = max(1, int(reject_after))
        self.require_generalization = bool(require_generalization)
        # 3.2B：与已晋升策略冲突时需更高层级验证（默认 5 次成功）才能覆盖
        self.conflict_threshold = max(1, int(conflict_threshold))
        self.conflict_detector = conflict_detector
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
            "origin_instruction": str(instruction or "")[:200],
            "verified_successes": 0,
            "verified_scenes": {SCENE_SIMILAR: 0, SCENE_RELATED: 0,
                                SCENE_DISTANT: 0},
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

    # ---- 3.2B 冲突检测 ----
    def conflicts_with(self, pid: str) -> List[str]:
        """返回与待晋升提议冲突的已晋升策略 id。

        默认确定性规则：同类别 + 共享触发关键词 + 措施不同即视为冲突；
        传入 conflict_detector 时优先使用（LLM 判定，失败回退确定性规则）。
        """
        p = self._data["proposals"].get(pid)
        if p is None:
            return []
        promoted = [q for q in self._data["proposals"].values()
                    if q.get("status") == STATUS_PROMOTED]
        if self.conflict_detector is not None:
            try:
                result = self.conflict_detector(p, promoted)
                if isinstance(result, (list, tuple, set)):
                    return [str(x) for x in result]
                if result:
                    return [q["id"] for q in promoted]
                return []
            except Exception as e:
                logger.warning("LLM 冲突检测失败，回退确定性规则: %s", e)
        out: List[str] = []
        for q in promoted:
            if q["id"] == pid:
                continue
            same_cat = (q.get("category") == p.get("category"))
            share_kw = bool((set(q.get("trigger_keywords") or [])
                             & set(p.get("trigger_keywords") or [])))
            same_action = ((q.get("action") or "") == (p.get("action") or ""))
            if same_cat and share_kw and not same_action:
                out.append(q["id"])
        return out

    def conflict_report(self, pid: str) -> Dict[str, Any]:
        """提议冲突状态（供决策日志 / TUI / Web 面板展示）。"""
        p = self._data["proposals"].get(pid)
        if p is None:
            return {}
        return {
            "conflicts": self.conflicts_with(pid),
            "threshold": self.conflict_threshold,
            "promotable": (int(p.get("verified_successes", 0))
                           >= self.conflict_threshold),
        }

    # ---- 验证 ----
    def verify(self, pid: str, ok: bool, instruction: str = "",
               require_generalization: Optional[bool] = None) -> str:
        """应用目标提议后验证（3.2A 泛化性测试）。

        成功计数并按验证场景落桶（similar/related/distant）；晋升需同时满足：
        连续成功达到阈值，且至少在一个“相似（>=0.7）”和一个“相关（0.4~0.7）”
        场景验证有效。仅单一场景有效的提议在应用达到上限后降级为项目级经验
        （STATUS_LOCAL），零成功则照旧丢弃（STATUS_REJECTED）。
        """
        p = self._data["proposals"].get(pid)
        if p is None or p.get("status") != STATUS_PENDING:
            return str(p.get("status", "")) if p else ""
        need_general = (self.require_generalization
                        if require_generalization is None
                        else bool(require_generalization))
        p["applied_count"] = int(p.get("applied_count", 0)) + 1
        if ok:
            if instruction:
                sim = scene_similarity(
                    p.get("origin_instruction", ""), instruction)
                bucket = scene_bucket(sim)
            else:
                # 未提供验证场景时默认视为“诞生场景”重复验证
                bucket = SCENE_SIMILAR
            scenes = p.setdefault("verified_scenes", {})
            scenes[bucket] = int(scenes.get(bucket, 0)) + 1
            p["verified_successes"] = int(p.get("verified_successes", 0)) + 1
            if (p["verified_successes"] >= self.promote_threshold
                    and (not need_general or self._generalized(p))):
                conflicts = self.conflicts_with(pid)
                if conflicts:
                    # 3.2B：与已晋升策略冲突，需更高层级验证才能覆盖旧策略
                    p["conflict_with"] = conflicts
                    if p["verified_successes"] < self.conflict_threshold:
                        self._save()
                        return STATUS_PENDING
                p["status"] = STATUS_PROMOTED
                p["promoted_at"] = time.time()
                self._save()
                return STATUS_PROMOTED
        if int(p.get("applied_count", 0)) >= self.reject_after:
            if int(p.get("verified_successes", 0)) >= 1:
                # 有成功但未覆盖相似+相关两个场景 -> 降级为项目级经验
                p["status"] = STATUS_LOCAL
                p["demoted_at"] = time.time()
            else:
                p["status"] = STATUS_REJECTED
                p["rejected_at"] = time.time()
            self._save()
            return p["status"]
        self._save()
        return p["status"]

    @staticmethod
    def _generalized(p: Dict[str, Any]) -> bool:
        """是否已在相似与相关两个场景各至少验证成功一次。"""
        scenes = p.get("verified_scenes") or {}
        return (int(scenes.get(SCENE_SIMILAR, 0)) >= 1
                and int(scenes.get(SCENE_RELATED, 0)) >= 1)

    def scene_report(self, pid: str) -> Dict[str, Any]:
        """提议的泛化验证进展（供决策日志 / TUI 展示）。"""
        p = self._data["proposals"].get(pid)
        if p is None:
            return {}
        scenes = p.get("verified_scenes") or {}
        return {
            "similar": int(scenes.get(SCENE_SIMILAR, 0)),
            "related": int(scenes.get(SCENE_RELATED, 0)),
            "distant": int(scenes.get(SCENE_DISTANT, 0)),
            "generalized": self._generalized(p),
        }

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
                                 STATUS_REJECTED, STATUS_LOCAL)}
        for p in self._data["proposals"].values():
            s = p.get("status")
            if s in counts:
                counts[s] += 1
        return counts

    def close(self) -> None:
        self._save()
