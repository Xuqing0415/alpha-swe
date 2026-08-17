# -*- coding: utf-8 -*-
"""多 Agent 共享项目记忆（交叉集成：项目记忆 x 多 Agent 共享）。

- 所有 Worker 读写同一个项目记忆后端（.swe-agent/memory/）；
- 写操作串行化：按后端 db_path 共享一把线程锁，避免并发写入竞争
  （同一进程内多 Agent 模拟；跨进程时由 SQLite/Chroma 自身事务兜底）；
- 创建者元数据：remember 自动标记 creator（Agent/角色身份），
  检索结果可供后续按创建者过滤；
- 私有记忆：metadata["private"]=True 的记忆仅创建者可见。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from agent.memory.store import MemoryStore

logger = logging.getLogger("alpha-swe.memory.shared")

# 后端级共享写锁：同一 db_path 的所有 Agent 实例共用一把锁（单进程多 Agent）
_LOCK_REGISTRY: Dict[str, threading.Lock] = {}
_REGISTRY_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _REGISTRY_GUARD:
        lock = _LOCK_REGISTRY.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCK_REGISTRY[key] = lock
        return lock


class SharedMemoryStore(MemoryStore):
    """共享记忆包装：写串行化 + 创建者标记 + 私有可见性过滤。

    包装外层 Store（如 LayeredMemoryStore），对 write 路径加锁并把
    creator 写入 metadata；检索时按 private/creator 过滤不可见条目。
    """

    def __init__(self, inner: MemoryStore, creator: str = "",
                 lock_key: Optional[str] = None) -> None:
        self.inner = inner
        self.creator = creator or ""
        self._lock = _lock_for(lock_key or id(inner))

    # ---- 写（串行化 + 创建者标记） ----
    def remember(self, kind: str, text: str,
                 metadata: Optional[Dict[str, Any]] = None) -> None:
        meta = dict(metadata or {})
        if self.creator:
            meta.setdefault("creator", self.creator)
        with self._lock:
            self.inner.remember(kind, text, meta)

    # ---- 检索（私有可见性过滤） ----
    def _filter_visible(self, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self.creator:
            return hits or []
        out = []
        for h in hits or []:
            meta = h.get("metadata") or {}
            if meta.get("private") and meta.get("creator") != self.creator:
                continue
            out.append(h)
        return out

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self._filter_visible(self.inner.retrieve(query, top_k))

    def search(self, query: str, top_k: int = 5,
               kinds: Optional[List[str]] = None,
               metadata_filter: Optional[Dict[str, Any]] = None
               ) -> List[Dict[str, Any]]:
        return self._filter_visible(
            self.inner.search(query, top_k, kinds, metadata_filter))

    def find_similar(self, text: str, top_k: int = 1,
                     kinds: Optional[List[str]] = None
                     ) -> List[Dict[str, Any]]:
        return self._filter_visible(self.inner.find_similar(text, top_k, kinds))

    def bump(self, memory_id: Any) -> None:
        self.inner.bump(memory_id)

    def close(self) -> None:
        self.inner.close()

    @property
    def disabled(self) -> bool:
        return getattr(self.inner, "disabled", False)
