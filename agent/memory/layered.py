"""项目级记忆分层（主线一 1.3）。

三层记忆：
- 会话状态（session）：当前任务的临时信息，内存驻留，随会话结束丢弃；
- 项目知识（project）：当前项目特有，`.swe-agent/memory/` 随项目走；
- 全局经验（global）：跨项目通用，`~/.swe-agent/memory/` 用户全局目录。

检索优先级：会话状态 > 项目知识 > 全局经验（同层内保持相关性排序）。
经验晋升：项目层经验被 >= promotion_threshold 个不同项目成功应用（检索命中）
后自动复制到全局层，实现"跨项目可迁移的通用经验"。

主线一 1.3A（记忆 TTL，默认关闭）：开启 ttl_enabled=True 后按文本 sha1
维护访问台账（global_meta_dir/access.json）：project/global 层命中记账
access_count/last_accessed；超过 ttl_cold_days 未访问的冷记忆检索降权
（score * cold_penalty 并附 "cold": True）；cleanup_candidates()/
note_session_start() 提示超过 ttl_cleanup_days 未访问的项目记忆待清理。
默认关闭时完全不产生台账，检索/排序/晋升行为不变。

主线一 1.3B（严格晋升，默认关闭）：开启 strict_promotion=True 后，项目层
经验晋升全局层需同时满足：不同项目应用数 >= promotion_threshold、各次应用
task_type 均非空且一致、应用样本两两 token Jaccard >=
promotion_min_context_similarity、至少一次 after_failure=True（失败后应用
成功的恢复证据）。promotion_readiness() 可查看就绪度。写入项目经验时建议
metadata 约定：{"layer": "project", "task_type": "debug",
"after_failure": True}，其中 layer 由 remember() 弹出决定路由，task_type 与
after_failure 用于严格晋升判定（缺失时记 None/False）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory.store import MemoryStore

logger = logging.getLogger("alpha-swe.memory.layered")

_LAYER_LABELS = {"session": "会话", "project": "项目", "global": "全局"}
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_\u4e00-\u9fff]+")


def _hash_text(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:16]


def _tokenize(text: str) -> set:
    return set(_TOKEN_RE.findall(str(text or "").lower()))


class SessionMemoryStore(MemoryStore):
    """会话级记忆：内存驻留，按关键词重叠打分（当前任务临时信息）。"""

    def __init__(self) -> None:
        self._items: List[Dict[str, Any]] = []
        self._seq = 0

    def remember(self, kind: str, text: str,
                 metadata: Optional[Dict[str, Any]] = None) -> None:
        self._seq += 1
        self._items.append({
            "id": self._seq,
            "kind": kind,
            "text": str(text),
            "metadata": dict(metadata or {}),
            "score": 0.0,
            "layer": "session",
        })

    def search(self, query: str, top_k: int = 5,
               kinds: Optional[List[str]] = None,
               metadata_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        q = _tokenize(query)
        scored = []
        for item in reversed(self._items):  # 最近写入优先
            if kinds and item["kind"] not in kinds:
                continue
            if metadata_filter:
                meta = item.get("metadata") or {}
                if not all(meta.get(k) == v for k, v in metadata_filter.items()):
                    continue
            # 子串匹配打分：任一查询词命中即计分（中文连续文本也可命中）
            low = str(item["text"]).lower()
            hits = sum(1 for term in q if term in low) if q else 0
            score = hits / len(q) if q else 0.0
            scored.append({**item, "score": score})
        scored.sort(key=lambda h: (h["score"], h["id"]), reverse=True)
        return scored[:top_k]

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self.search(query, top_k)

    def find_similar(self, text: str, top_k: int = 1,
                     kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        return self.search(text, top_k, kinds)

    def close(self) -> None:
        self._items.clear()


class LayeredMemoryStore(MemoryStore):
    """三层记忆包装器：会话 > 项目 > 全局，带跨项目晋升。

    可选能力（均默认关闭，不影响既有行为）：
    - ttl_enabled：访问台账（access.json）记账、冷记忆降权、待清理提示；
    - strict_promotion：晋升前校验任务类型一致性 / 上下文相似度 / 恢复证据。
    """

    def __init__(
        self,
        project_store: MemoryStore,
        global_store: MemoryStore,
        project_key: str = "",
        global_meta_dir: Optional[str] = None,
        promotion_threshold: int = 3,
        session_store: Optional[MemoryStore] = None,
        ttl_enabled: bool = False,
        ttl_cold_days: float = 30.0,
        ttl_cleanup_days: float = 90.0,
        cold_penalty: float = 0.5,
        strict_promotion: bool = False,
        promotion_min_context_similarity: float = 0.6,
        promotion_require_failure_recovery: bool = True,
    ) -> None:
        self.project_store = project_store
        self.global_store = global_store
        self.session_store = session_store or SessionMemoryStore()
        self.project_key = project_key
        self.promotion_threshold = max(1, int(promotion_threshold))
        self.ttl_enabled = bool(ttl_enabled)
        self.ttl_cold_days = float(ttl_cold_days)
        self.ttl_cleanup_days = float(ttl_cleanup_days)
        self.cold_penalty = float(cold_penalty)
        self.strict_promotion = bool(strict_promotion)
        self.promotion_min_context_similarity = float(
            promotion_min_context_similarity)
        self.promotion_require_failure_recovery = bool(
            promotion_require_failure_recovery)
        self._promo_path = (
            Path(global_meta_dir) / "promotions.json"
            if global_meta_dir else None
        )
        self._promotions: Dict[str, Dict[str, Any]] = self._load_promotions()
        self._promo_dirty = False
        # 访问台账仅在 ttl_enabled=True 时存在，避免默认路径产生任何文件
        self._access_path = (
            Path(global_meta_dir) / "access.json"
            if (global_meta_dir and self.ttl_enabled) else None
        )
        self._ledger: Dict[str, Dict[str, Any]] = (
            self._load_ledger() if self.ttl_enabled else {}
        )
        self._ledger_dirty = False

    # ---- 加载 / 保存晋升台账 ----
    def _load_promotions(self) -> Dict[str, Dict[str, Any]]:
        if self._promo_path is None:
            return {}
        try:
            data = json.loads(self._promo_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _flush_promotions(self) -> None:
        if not self._promo_dirty or self._promo_path is None:
            return
        try:
            self._promo_path.parent.mkdir(parents=True, exist_ok=True)
            self._promo_path.write_text(
                json.dumps(self._promotions, ensure_ascii=False, indent=2),
                encoding="utf-8")
            self._promo_dirty = False
        except OSError as e:
            logger.warning("晋升台账写入失败: %s", e)

    # ---- 加载 / 保存访问台账（1.3A TTL） ----
    def _load_ledger(self) -> Dict[str, Dict[str, Any]]:
        if self._access_path is None:
            return {}
        try:
            data = json.loads(self._access_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _flush_ledger(self) -> None:
        if not self._ledger_dirty or self._access_path is None:
            return
        try:
            self._access_path.parent.mkdir(parents=True, exist_ok=True)
            self._access_path.write_text(
                json.dumps(self._ledger, ensure_ascii=False, indent=2),
                encoding="utf-8")
            self._ledger_dirty = False
        except OSError as e:
            logger.warning("访问台账写入失败: %s", e)

    @staticmethod
    def _days_idle(iso_value: Any) -> float:
        """距台账时间戳的闲置天数（解析失败视为未闲置）。"""
        try:
            last = datetime.fromisoformat(str(iso_value or ""))
        except (ValueError, TypeError):
            return 0.0
        return max(0.0, (datetime.now() - last).total_seconds() / 86400.0)

    def _track_access(self, hit: Dict[str, Any]) -> None:
        """对被实际返回的 project/global 层命中记账并落盘。"""
        if not self.ttl_enabled:
            return
        if hit.get("layer") not in ("project", "global"):
            return
        key = _hash_text(hit.get("text", ""))
        now = datetime.now().isoformat(timespec="seconds")
        entry = self._ledger.get(key)
        if entry is None:
            entry = self._ledger[key] = {
                "kind": hit.get("kind", ""),
                "layer": hit.get("layer"),
                "access_count": 0,
                "last_accessed": now,
                "first_seen": now,
                "projects": [],
            }
        entry["access_count"] = int(entry.get("access_count") or 0) + 1
        entry["last_accessed"] = now
        if hit.get("layer") == "project" and self.project_key:
            pk = str(self.project_key)
            projects = entry.setdefault("projects", [])
            if pk not in projects:
                projects.append(pk)
        self._ledger_dirty = True
        self._flush_ledger()

    def _apply_ttl_ranking(self, hits: List[Dict[str, Any]]) -> None:
        """ttl_enabled=True 专属：冷记忆降权，同分按热度（access_count）排序。"""
        for h in hits:
            if h.get("layer") not in ("project", "global"):
                continue
            entry = self._ledger.get(_hash_text(h.get("text", "")))
            if entry is None:
                continue
            if self._days_idle(entry.get("last_accessed")) > self.ttl_cold_days:
                h["score"] = round(
                    float(h.get("score", 0) or 0) * self.cold_penalty, 4)
                h["cold"] = True

        def _sort_key(h: Dict[str, Any]):
            score = float(h.get("score", 0) or 0)
            if h.get("layer") in ("project", "global"):
                entry = self._ledger.get(_hash_text(h.get("text", "")))
                access = int((entry or {}).get("access_count") or 0)
            else:
                access = 0
            return (score, access)

        hits.sort(key=_sort_key, reverse=True)

    def cleanup_candidates(self) -> List[Dict[str, Any]]:
        """返回闲置 >= ttl_cleanup_days 的项目记忆候选（不做实际删除）。"""
        if not self.ttl_enabled:
            return []
        out = []
        for key, entry in self._ledger.items():
            if entry.get("layer") != "project":
                continue
            days = self._days_idle(entry.get("last_accessed"))
            if days >= self.ttl_cleanup_days:
                out.append({
                    "key": key,
                    "kind": entry.get("kind", ""),
                    "last_accessed": entry.get("last_accessed", ""),
                    "days_idle": round(days, 1),
                })
        out.sort(key=lambda c: float(c.get("days_idle") or 0), reverse=True)
        return out

    def note_session_start(self) -> str:
        """会话开始提示：有清理候选时返回一句提示，否则返回空串。"""
        n = len(self.cleanup_candidates())
        if not n:
            return ""
        return (f"检测到 {n} 条项目记忆超过 "
                f"{int(self.ttl_cleanup_days)} 天未访问，可考虑清理")

    # ---- 写入 ----
    def remember(self, kind: str, text: str,
                 metadata: Optional[Dict[str, Any]] = None) -> None:
        meta = dict(metadata or {})
        layer = meta.pop("layer", "project")
        target = {"session": self.session_store,
                  "global": self.global_store}.get(layer, self.project_store)
        target.remember(kind, text, meta)

    # ---- 检索（会话 > 项目 > 全局） ----
    def search(self, query: str, top_k: int = 5,
               kinds: Optional[List[str]] = None,
               metadata_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        session_hits = self._safe_search(
            self.session_store, query, top_k, kinds, metadata_filter, "session")
        project_hits = self._safe_search(
            self.project_store, query, top_k, kinds, metadata_filter, "project")
        global_hits = self._safe_search(
            self.global_store, query, top_k, kinds, metadata_filter, "global")
        # 相关性下限：score=0（无词命中）的结果不参与层优先级抢占，避免
        # 低相关会话项压过项目/全局层的精确匹配；全部为空时回退未过滤
        q_tokens = _tokenize(query)
        if q_tokens:
            pruned = [[h for h in hits if float(h.get("score", 0) or 0) > 0]
                      for hits in (session_hits, project_hits, global_hits)]
            if any(pruned):
                session_hits, project_hits, global_hits = pruned
        for h in project_hits:
            self._track_promotion(h)
        if self.ttl_enabled:
            self._apply_ttl_ranking(project_hits)
            self._apply_ttl_ranking(global_hits)
        merged: List[Dict[str, Any]] = []
        seen = set()
        for h in session_hits + project_hits + global_hits:
            key = (h.get("kind"), _hash_text(h.get("text", "")))
            if key in seen:
                continue
            seen.add(key)
            merged.append(h)
            if len(merged) >= top_k:
                break
        for h in merged:
            self._track_access(h)
        return merged

    @staticmethod
    def _safe_search(store: MemoryStore, query: str, top_k: int,
                     kinds, metadata_filter, layer: str) -> List[Dict[str, Any]]:
        if getattr(store, "disabled", False):
            return []
        try:
            hits = store.search(query, top_k=top_k, kinds=kinds,
                                metadata_filter=metadata_filter)
            for h in hits or []:
                h["layer"] = layer
            return hits or []
        except Exception as e:
            logger.warning("记忆分层检索失败（%s）: %s", layer, e)
            return []

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self.search(query, top_k)

    def find_similar(self, text: str, top_k: int = 1,
                     kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        best: List[Dict[str, Any]] = []
        for store, layer in ((self.session_store, "session"),
                             (self.project_store, "project"),
                             (self.global_store, "global")):
            if getattr(store, "disabled", False):
                continue
            try:
                for h in store.find_similar(text, top_k=top_k, kinds=kinds) or []:
                    h["layer"] = layer
                    best.append(h)
            except Exception as e:
                logger.warning("记忆去重检索失败（%s）: %s", layer, e)
        if self.ttl_enabled:
            self._apply_ttl_ranking(best)
        else:
            best.sort(key=lambda h: float(h.get("score", 0) or 0),
                      reverse=True)
        returned = best[:top_k]
        for h in returned:
            self._track_access(h)
        return returned

    def bump(self, memory_id: Any) -> None:
        for store in (self.session_store, self.project_store,
                      self.global_store):
            if getattr(store, "disabled", False):
                continue
            try:
                store.bump(memory_id)
            except Exception:
                pass

    def format_context(self, hits: List[Dict[str, Any]]) -> str:
        if not hits:
            return ""
        lines = []
        for h in hits:
            kind = h.get("kind", "")
            text = str(h.get("text", ""))
            meta = h.get("metadata") or {}
            path = meta.get("path", "")
            layer = _LAYER_LABELS.get(h.get("layer", "project"), "project")
            head = f"[{layer}][{kind}]" + (f" {path}" if path else "")
            if h.get("cold"):
                head += "（冷记忆）"
            lines.append(f"- {head} {text[:300]}")
        return "\n".join(lines)

    # ---- 跨项目晋升 ----
    def _track_promotion(self, hit: Dict[str, Any]) -> None:
        if not self.project_key:
            return
        if hit.get("kind") != "experience":
            return
        if getattr(self.global_store, "disabled", False):
            return
        key = _hash_text(hit.get("text", ""))
        entry = self._promotions.get(key)
        if entry is None:
            entry = self._promotions[key] = {"projects": [], "promoted": False}
        if entry.get("promoted"):
            return
        pk = str(self.project_key)
        if pk not in entry["projects"]:
            entry["projects"].append(pk)
            meta = hit.get("metadata") or {}
            entry.setdefault("apps", []).append({
                "project": pk,
                "task_type": meta.get("task_type"),
                "sample": str(hit.get("text", ""))[:80],
                "after_failure": bool(meta.get("after_failure", False)),
            })
            self._promo_dirty = True
        if len(entry["projects"]) >= self.promotion_threshold:
            if self.strict_promotion:
                status = self._strict_promotion_status(key, entry)
                if not status["ready"]:
                    logger.info(
                        "经验晋升条件不满足（1.3B），留在项目层: %s（%s）",
                        key, "；".join(status["missing"]))
                    self._flush_promotions()
                    return
            try:
                meta = dict(hit.get("metadata") or {})
                meta["promoted"] = True
                meta["source_project"] = pk
                self.global_store.remember("experience", hit.get("text", ""), meta)
                entry["promoted"] = True
                self._promo_dirty = True
                logger.info(
                    "经验晋升全局层: 已在 %d 个项目被应用（%s）",
                    len(entry["projects"]), key)
            except Exception as e:
                logger.warning("经验晋升全局失败: %s", e)
        self._flush_promotions()

    @staticmethod
    def _min_sample_similarity(apps: List[Dict[str, Any]]) -> float:
        """任意两条跨项目应用 sample 的 token Jaccard 最小值。"""
        samples = [str(a.get("sample") or "") for a in (apps or [])]
        if len(samples) < 2:
            return 1.0
        best = 1.0
        for i in range(len(samples)):
            for j in range(i + 1, len(samples)):
                toks_i = _tokenize(samples[i])
                toks_j = _tokenize(samples[j])
                union = toks_i | toks_j
                sim = (len(toks_i & toks_j) / len(union)) if union else 0.0
                best = min(best, sim)
        return best

    def _strict_promotion_status(
            self, key: str,
            entry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """计算某条经验的严格晋升就绪度（_track_promotion 与 readiness 共用）。"""
        if entry is None:
            entry = self._promotions.get(key) or {}
        projects = [str(p) for p in (entry.get("projects") or [])]
        apps: List[Dict[str, Any]] = list(entry.get("apps") or [])
        reasons: List[str] = []

        enough_projects = len(projects) >= self.promotion_threshold
        if not enough_projects:
            reasons.append(
                f"应用项目数不足（{len(projects)}/{self.promotion_threshold}）")

        if apps:
            task_types = [a.get("task_type") for a in apps]
            types_ok = all(isinstance(t, str) and t for t in task_types)
            task_type_consistent = types_ok and len(set(task_types)) == 1
            if not task_type_consistent:
                reasons.append("任务类型缺失或不一致")
        else:
            task_type_consistent = True

        min_similarity = self._min_sample_similarity(apps)
        if apps and min_similarity < self.promotion_min_context_similarity:
            reasons.append(
                f"上下文相似度不足（{min_similarity:.2f} < "
                f"{self.promotion_min_context_similarity:.2f}）")

        has_recovery = any(bool(a.get("after_failure")) for a in apps)
        if (self.promotion_require_failure_recovery and apps
                and not has_recovery):
            reasons.append("缺少失败后应用成功的恢复证据")

        return {
            "projects": projects,
            "task_type_consistent": task_type_consistent,
            "min_similarity": round(min_similarity, 4),
            "has_recovery": has_recovery,
            "ready": not reasons,
            "missing": reasons,
        }

    def promotion_readiness(self, key_text: str) -> Dict[str, Any]:
        """返回严格晋升就绪度，供决策日志 / TUI 展示。"""
        return self._strict_promotion_status(
            key=_hash_text(str(key_text or "")))

    # ---- 生命周期 ----
    @property
    def disabled(self) -> bool:
        return bool(getattr(self.project_store, "disabled", False) and
                    getattr(self.global_store, "disabled", False))

    def close(self) -> None:
        self._flush_promotions()
        self._flush_ledger()
        for store in (self.session_store, self.project_store,
                      self.global_store):
            closer = getattr(store, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass
