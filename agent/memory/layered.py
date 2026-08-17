"""项目级记忆分层（主线一 1.3）。

三层记忆：
- 会话状态（session）：当前任务的临时信息，内存驻留，随会话结束丢弃；
- 项目知识（project）：当前项目特有，`.swe-agent/memory/` 随项目走；
- 全局经验（global）：跨项目通用，`~/.swe-agent/memory/` 用户全局目录。

检索优先级：会话状态 > 项目知识 > 全局经验（同层内保持相关性排序）。
经验晋升：项目层经验被 >= promotion_threshold 个不同项目成功应用（检索命中）
后自动复制到全局层，实现"跨项目可迁移的通用经验"。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
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
    """三层记忆包装器：会话 > 项目 > 全局，带跨项目晋升。"""

    def __init__(
        self,
        project_store: MemoryStore,
        global_store: MemoryStore,
        project_key: str = "",
        global_meta_dir: Optional[str] = None,
        promotion_threshold: int = 3,
        session_store: Optional[MemoryStore] = None,
    ) -> None:
        self.project_store = project_store
        self.global_store = global_store
        self.session_store = session_store or SessionMemoryStore()
        self.project_key = project_key
        self.promotion_threshold = max(1, int(promotion_threshold))
        self._promo_path = (
            Path(global_meta_dir) / "promotions.json"
            if global_meta_dir else None
        )
        self._promotions: Dict[str, Dict[str, Any]] = self._load_promotions()
        self._promo_dirty = False

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
        best.sort(key=lambda h: float(h.get("score", 0) or 0), reverse=True)
        return best[:top_k]

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
            self._promo_dirty = True
        if len(entry["projects"]) >= self.promotion_threshold:
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

    # ---- 生命周期 ----
    @property
    def disabled(self) -> bool:
        return bool(getattr(self.project_store, "disabled", False) and
                    getattr(self.global_store, "disabled", False))

    def close(self) -> None:
        self._flush_promotions()
        for store in (self.session_store, self.project_store,
                      self.global_store):
            closer = getattr(store, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass