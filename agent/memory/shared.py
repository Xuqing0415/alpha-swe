# -*- coding: utf-8 -*-
"""多 Agent 共享项目记忆（交叉集成：项目记忆 x 多 Agent 共享）。深化：写入冲突仲裁。

- 所有 Worker 读写同一个项目记忆后端（.swe-agent/memory/）；
- 写操作串行化：按后端 db_path 共享一把线程锁，避免并发写入竞争
  （同一进程内多 Agent 模拟；跨进程时由 SQLite/Chroma 自身事务兜底）；
- 创建者元数据：remember 自动标记 creator（Agent/角色身份），
  检索结果可供后续按创建者过滤；
- 私有记忆：metadata["private"]=True 的记忆仅创建者可见；
- 写入冲突仲裁（深化）：锁内先做确定性去重——相同知识点已存在时：
  * 同创建者重复：与主流程去重语义一致，只 bump 已有条目，不重复写入；
  * 跨 Agent 碰撞：创建者不同但内容近乎重复，不重复写入，bump 已有条目
    并记录 arbitration 决策（避免多个 Agent 各自写一份相似记忆污染检索）；
- close() 引用计数保护：同一后端被多个 wrapper 包装时，只有最后一个
  wrapper 关闭才真正关闭底层后端，防止"先关的 worker 把仍在写入的
  worker 的后端提前关闭"。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from agent.memory.store import MemoryStore

logger = logging.getLogger("alpha-swe.memory.shared")

# 后端级共享写锁：同一 db_path 的所有 Agent 实例共用一把锁（单进程多 Agent）
_LOCK_REGISTRY: Dict[str, threading.Lock] = {}
# 后端级 wrapper 引用计数：{lock_key: {id(inner): 包装同一 inner 的 wrapper 数}}
_WRAPPER_COUNTS: Dict[str, Dict[int, int]] = {}
_REGISTRY_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _REGISTRY_GUARD:
        lock = _LOCK_REGISTRY.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCK_REGISTRY[key] = lock
        return lock


class SharedMemoryStore(MemoryStore):
    """共享记忆包装：写串行化 + 创建者标记 + 私有可见性过滤 + 写入冲突仲裁。

    包装外层 Store（如 LayeredMemoryStore），对 write 路径加锁并把
    creator 写入 metadata；检索时按 private/creator 过滤不可见条目。
    """

    def __init__(self, inner: MemoryStore, creator: str = "",
                 lock_key: Optional[str] = None,
                 dedup_threshold: float = 0.95,
                 max_arbitration_records: int = 20) -> None:
        self.inner = inner
        self.creator = creator or ""
        self.dedup_threshold = max(0.0, float(dedup_threshold))
        self._lock_key = lock_key if lock_key is not None else id(inner)
        self._lock = _lock_for(self._lock_key)
        self._closed = False
        # 仲裁可观测性：跨 Agent 碰撞次数 + 最近决策（deque，避免无限累积）
        self.arbitration_count = 0
        self._arbitrations: Deque[Dict[str, Any]] = deque(
            maxlen=max(1, int(max_arbitration_records)))
        with _REGISTRY_GUARD:
            counts = _WRAPPER_COUNTS.setdefault(self._lock_key, {})
            counts[id(inner)] = counts.get(id(inner), 0) + 1

    # ---- 写（串行化 + 创建者标记 + 冲突仲裁） ----
    def remember(self, kind: str, text: str,
                 metadata: Optional[Dict[str, Any]] = None) -> None:
        meta = dict(metadata or {})
        if self.creator:
            meta.setdefault("creator", self.creator)
        with self._lock:
            if self._maybe_arbitrate(kind, text):
                return
            self.inner.remember(kind, text, meta)

    def _maybe_arbitrate(self, kind: str, text: str) -> bool:
        """锁内确定性写冲突仲裁：返回 True 表示已仲裁处理（无需写入）。"""
        if self.dedup_threshold <= 0:
            return False
        try:
            hits = self.inner.find_similar(text, top_k=1, kinds=[kind])
        except Exception as e:  # 去重/仲裁查询失败不应阻断正常写入
            logger.debug("写入仲裁去重查询失败: %s", e)
            return False
        if not hits:
            return False
        best = hits[0]
        score = float(best.get("score") or 0)
        if score < self.dedup_threshold:
            return False
        memory_id = best.get("id")
        if memory_id is not None:
            try:
                self.inner.bump(memory_id)
            except Exception as e:
                logger.warning("仲裁 bump 失败: %s", e)
        hit_meta = best.get("metadata") or {}
        existing_creator = (hit_meta.get("creator") or "").strip()
        if existing_creator and existing_creator != self.creator:
            # 跨 Agent 写入同一知识点：不重复写入，bump 已有条目并记录决策
            self.arbitration_count += 1
            record = {
                "kind": kind,
                "score": round(score, 4),
                "existing_creator": existing_creator,
                "incoming_creator": self.creator or "",
                "memory_id": memory_id,
                "action": "bump_existing",
            }
            self._arbitrations.append(record)
            logger.warning(
                "共享记忆写入冲突仲裁: %r 与现有创建者 %r 写入相似内容"
                "（score=%.3f, id=%s），bump 已有条目，跳过重复写入",
                self.creator, existing_creator, score, memory_id)
        else:
            logger.debug("共享记忆去重: score=%.3f, bump id=%s",
                         score, memory_id)
        return True

    @property
    def arbitrations(self) -> List[Dict[str, Any]]:
        """最近写入冲突仲裁决策（新 -> 旧）。"""
        return list(self._arbitrations)

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
        if self._closed:
            return
        self._closed = True
        close_inner = False
        with _REGISTRY_GUARD:
            counts = _WRAPPER_COUNTS.get(self._lock_key)
            if counts is not None:
                remaining = counts.get(id(self.inner), 1) - 1
                if remaining <= 0:
                    counts.pop(id(self.inner), None)
                    close_inner = True
                    if not counts:
                        _WRAPPER_COUNTS.pop(self._lock_key, None)
                else:
                    counts[id(self.inner)] = remaining
        if close_inner:
            try:
                self.inner.close()
            except Exception as e:
                logger.warning("共享记忆关闭失败: %s", e)

    @property
    def disabled(self) -> bool:
        return getattr(self.inner, "disabled", False)