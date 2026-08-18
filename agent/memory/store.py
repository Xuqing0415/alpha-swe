"""长期记忆存储（升级版）—— 对应设计第 7 节。

- 向量检索：Hybrid 本地后端（TF-IDF + 关键词混合打分，零新依赖）；
  Chroma / Qdrant 后端可插拔（安装对应包后自动可用）。
- 自动写入：经验摘要（remember_experience）、代码索引（index_code）、
  错误记忆（remember_error）。
- 统一接口：remember / retrieve / search / close，SQLite 后端保持兼容。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from agent.memory.embed import Embedder, TfidfEmbedder

logger = logging.getLogger("alpha-swe.memory")


def vector_db_dir(db_path: str, backend: str) -> str:
    """向量库（chroma/qdrant）持久化目录。

    db_path 默认为 "memory.db"（sqlite/hybrid 文件形态）；而 chroma/qdrant 的
    PersistentClient 需要目录路径。若 db_path 带后缀（文件形态），派生同名前缀的
    独立目录（memory.db -> memory.chroma），避免"文件已存在"冲突（os error 183）。
    """
    p = Path(db_path)
    if p.suffix:
        return str(p.parent / f"{p.stem}.{backend}")
    return str(p / backend)

SYMBOL_PATTERN = re.compile(
    r"\b(?:class|def|async def|function|async function|const|let|var|"
    r"export function|export const)\s+([A-Za-z_]\w*)"
)


def extract_symbols(text: str, max_symbols: int = 50) -> List[str]:
    """从代码文本中提取符号名（类/函数/变量声明）。"""
    seen: List[str] = []
    for m in SYMBOL_PATTERN.finditer(text):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
            if len(seen) >= max_symbols:
                break
    return seen


class MemoryStore(ABC):
    """记忆存储统一接口。"""

    @abstractmethod
    def remember(self, kind: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """写入一条记忆（kind: experience|code|error|note）。"""

    @abstractmethod
    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """按相关性检索（兼容旧接口，等价于 search 不带过滤）。"""

    @abstractmethod
    def search(self, query: str, top_k: int = 5,
               kinds: Optional[List[str]] = None,
               metadata_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """混合检索：向量相似度 + 关键词，可按 kind 与元数据过滤。"""

    @abstractmethod
    def close(self) -> None:
        """释放后端资源。"""

    @property
    def disabled(self) -> bool:
        """记忆是否被禁用（Noop 后端返回 True）。"""
        return False

    def find_similar(self, text: str, top_k: int = 1,
                     kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """查找与 text 最相似的已有记忆（写入前去重用）；默认后端不支持。"""
        return []

    def bump(self, memory_id: Any) -> None:  # noqa: B027 可选钩子默认空实现
        """记忆被引用/去重命中时更新使用计数与时间；默认不记录。"""

    # ---- 便捷写入（子类复用 remember 即可） ----
    def index_code(self, path: str, content: str, symbols: Optional[List[str]] = None,
                   metadata: Optional[Dict[str, Any]] = None,
                   max_chars: int = 2000) -> None:
        """代码索引：关联文件路径与符号。"""
        content = content or ""
        if not content.strip():
            return
        symbols = symbols or extract_symbols(content)
        meta: Dict[str, Any] = {"path": path, "symbols": symbols}
        if metadata:
            meta.update(metadata)
        excerpt = content[:max_chars]
        text = f"文件: {path}\n符号: {', '.join(symbols)}\n{excerpt}"
        self.remember("code", text, meta)

    def remember_experience(self, summary: Dict[str, Any]) -> None:
        """经验摘要写入（problem / steps / solution / outcome / key_files）。"""
        text = format_experience_text(summary)
        meta: Dict[str, Any] = {
            "key_files": summary.get("key_files", []),
            "task_type": summary.get("task_type") or classify_task_type(
                summary.get("problem", "")),
            "outcome": summary.get("outcome", "success"),
            "negative": False,
        }
        self.remember("experience", text, meta)

    def remember_error(self, error_type: str, stack: str,
                       solution: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> None:
        """错误记忆：错误类型 + 堆栈/上下文 + 最终解决方案。"""
        text = (
            f"错误类型: {error_type}\n"
            f"上下文: {stack}\n"
            f"解决: {solution or '（未解决）'}"
        )
        meta: Dict[str, Any] = dict(metadata or {})
        meta.setdefault("negative", True)  # 错误记忆作为反例，检索时降权
        self.remember("error", text, meta)

    def format_context(self, hits: List[Dict[str, Any]]) -> str:
        """把检索结果格式化为注入 Prompt 的文本块。"""
        if not hits:
            return ""
        lines = []
        for h in hits:
            kind = h.get("kind", "")
            text = str(h.get("text", ""))
            meta = h.get("metadata") or {}
            path = meta.get("path", "")
            head = f"[{kind}]" + (f" {path}" if path else "")
            lines.append(f"- {head} {text[:300]}")
        return "\n".join(lines)


class NoopMemoryStore(MemoryStore):
    """长期记忆已禁用（memory.backend = none）时的空实现。"""

    def __init__(self, *args, **kwargs):
        pass

    def remember(self, kind: str, text: str,
                 metadata: Optional[Dict[str, Any]] = None) -> None:
        pass

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return []

    def search(self, query: str, top_k: int = 5,
               kinds: Optional[List[str]] = None,
               metadata_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return []

    def find_similar(self, text: str, top_k: int = 1,
                     kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        return []

    def close(self) -> None:
        pass

    @property
    def disabled(self) -> bool:
        return True


class SqliteMemoryStore(MemoryStore):
    """SQLite 关键词检索（零依赖，兼容旧实现）。"""

    def __init__(self, db_path: str = "memory.db", max_entities: int = 1000,
                 decay_days: Optional[float] = None,
                 decay_factor: Optional[float] = None,
                 counter_example_penalty: Optional[float] = None):
        self.db_path = db_path
        self.max_entities = max_entities
        self.decay_days = decay_days if decay_days is not None else 30.0
        self.decay_factor = decay_factor if decay_factor is not None else 0.1
        self.counter_example_penalty = (
            counter_example_penalty if counter_example_penalty is not None else 0.3
        )
        self._conn = sqlite3.connect(db_path, check_same_thread=False,
                                     timeout=30)  # busy_timeout 30s
        # 多实例共享后端：WAL 允许并发读 + 单写者，减少 "database is locked"
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                use_count INTEGER DEFAULT 0,
                last_used_at TEXT
            )
        """)
        _ensure_columns(self._conn)
        self._conn.commit()

    def remember(self, kind: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT INTO memories (kind, text, metadata, last_used_at) VALUES (?, ?, ?, ?)",
            (kind, text, json.dumps(metadata or {}, ensure_ascii=False), now),
        )
        self._trim()
        self._conn.commit()

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self.search(query, top_k=top_k)

    def search(self, query: str, top_k: int = 5,
               kinds: Optional[List[str]] = None,
               metadata_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 1]
        rows = self._conn.execute(
            "SELECT id, kind, text, metadata, created_at, use_count, last_used_at "
            "FROM memories ORDER BY id DESC"
        ).fetchall()
        scored = []
        for rid, kind, text, metadata, created, use_count, last_used in rows:
            if kinds is not None and kind not in kinds:
                continue
            if metadata_filter and not _metadata_matches(metadata, metadata_filter):
                continue
            raw = sum(1 for t in terms if t in text.lower())
            if terms and raw == 0:
                continue
            meta = _parse_meta(metadata)
            adjusted = _decay_score(
                raw, use_count, last_used, created,
                decay_days=self.decay_days,
                decay_factor=self.decay_factor,
                counter_example_penalty=self.counter_example_penalty,
                negative=bool(meta.get("negative")),
            )
            if adjusted <= 0:
                continue
            is_neg = 0 if not bool(meta.get("negative")) else 1
            scored.append((is_neg, -adjusted, rid,
                           _hit(kind, text, metadata, created, adjusted)))
        scored.sort()
        top = scored[:top_k]
        for _neg, _adj, rid, _h in top:
            self.bump(rid)
        return [h for _neg, _adj, _, h in top]

    def find_similar(self, text: str, top_k: int = 1,
                     kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        terms = [t for t in re.split(r"\W+", text.lower()) if len(t) > 1]
        rows = self._conn.execute(
            "SELECT id, kind, text, metadata, created_at FROM memories ORDER BY id DESC"
        ).fetchall()
        scored = []
        for rid, kind, rtext, metadata, created in rows:
            if kinds is not None and kind not in kinds:
                continue
            score = sum(1 for t in terms if t in rtext.lower())
            if terms and score == 0:
                continue
            # 归一化为 0..1（匹配词占比），与 hybrid/向量后端的阈值语义一致
            norm = score / len(terms) if terms else 0.0
            scored.append((norm, rid, _hit(kind, rtext, metadata, created, norm)))
        scored.sort(key=lambda x: -x[0])
        return [dict(h, id=rid) for _, rid, h in scored[:top_k]]

    def bump(self, memory_id: int) -> None:
        try:
            now = datetime.now().isoformat(timespec="seconds")
            self._conn.execute(
                "UPDATE memories SET use_count = use_count + 1, last_used_at = ? "
                "WHERE id = ?",
                (now, memory_id),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.warning("记忆引用计数更新失败: %s", e)

    def close(self) -> None:
        self._conn.close()

    def _trim(self) -> None:
        total = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        if total > self.max_entities:
            self._conn.execute("""
                DELETE FROM memories WHERE id IN (
                    SELECT id FROM memories ORDER BY id ASC LIMIT ?
                )
            """, (total - self.max_entities,))
            self._conn.commit()


class VectorMemoryStore(MemoryStore):
    """带嵌入器的基础向量记忆。"""

    def __init__(self, embedder: Optional[Embedder] = None):
        self.embedder = embedder or TfidfEmbedder()


class HybridLocalMemoryStore(VectorMemoryStore):
    """本地混合检索：TF-IDF 向量 + 关键词打分，SQLite 持久化。

    适用于中小规模（数千条）；大规模应切换到 Chroma/Qdrant 后端。
    """

    def __init__(self, db_path: str = "memory.db", max_entities: int = 1000,
                 embedder: Optional[Embedder] = None,
                 vector_weight: float = 0.6,
                 max_code_chars: int = 2000,
                 decay_days: Optional[float] = None,
                 decay_factor: Optional[float] = None,
                 counter_example_penalty: Optional[float] = None):
        super().__init__(embedder)
        self.db_path = db_path
        self.max_entities = max_entities
        self.vector_weight = max(0.0, min(1.0, vector_weight))
        self.max_code_chars = max_code_chars
        self.decay_days = decay_days if decay_days is not None else 30.0
        self.decay_factor = decay_factor if decay_factor is not None else 0.1
        self.counter_example_penalty = (
            counter_example_penalty if counter_example_penalty is not None else 0.3
        )
        self._conn = sqlite3.connect(db_path, check_same_thread=False,
                                     timeout=30)  # busy_timeout 30s
        # 多实例共享后端：WAL 允许并发读 + 单写者，减少 "database is locked"
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                use_count INTEGER DEFAULT 0,
                last_used_at TEXT
            )
        """)
        _ensure_columns(self._conn)
        self._conn.commit()
        self._ids: List[int] = []
        self._texts: List[str] = []
        self._use_counts: List[int] = []
        self._last_used: List[Optional[str]] = []
        self._vectors: Optional[np.ndarray] = None
        self._dirty = False
        self._load()

    # ---- 内部 ----
    def _load(self) -> None:
        rows = self._conn.execute(
            "SELECT id, text, use_count, last_used_at FROM memories ORDER BY id"
        ).fetchall()
        self._ids = [r[0] for r in rows]
        self._texts = [r[1] for r in rows]
        self._use_counts = [r[2] or 0 for r in rows]
        self._last_used = [r[3] for r in rows]
        self._dirty = True

    def _ensure_vectors(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        if not self._texts:
            self._vectors = None
            return
        try:
            vecs = np.asarray(self.embedder.embed(self._texts), dtype=np.float64)
            self._vectors = vecs
        except Exception as e:
            logger.warning("向量化失败，回退关键词检索: %s", e)
            self._vectors = None

    # ---- 写入 ----
    def remember(self, kind: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT INTO memories (kind, text, metadata, last_used_at) VALUES (?, ?, ?, ?)",
            (kind, text, json.dumps(metadata or {}, ensure_ascii=False), now),
        )
        self._trim()
        self._conn.commit()
        row_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self._ids.append(row_id)
        self._texts.append(text)
        self._use_counts.append(0)
        self._last_used.append(now)
        self._dirty = True

    def index_code(self, path: str, content: str, symbols=None,
                   metadata: Optional[Dict[str, Any]] = None, max_chars: int = 0) -> None:
        super().index_code(path, content, symbols, metadata,
                           max_chars=max_chars or self.max_code_chars)

    # ---- 检索 ----
    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self.search(query, top_k=top_k)

    def search(self, query: str, top_k: int = 5,
               kinds: Optional[List[str]] = None,
               metadata_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, kind, text, metadata, created_at, use_count, last_used_at "
            "FROM memories ORDER BY id DESC"
        ).fetchall()
        rows = [
            r for r in rows
            if (kinds is None or r[1] in kinds)
            and (not metadata_filter or _metadata_matches(r[3], metadata_filter))
        ]
        if not rows:
            return []

        self._ensure_vectors()
        terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 1]
        id_index = {iid: i for i, iid in enumerate(self._ids)}

        query_vec: Optional[np.ndarray] = None
        if self._vectors is not None and self._vectors.shape[0] > 0:
            try:
                qv = np.asarray(self.embedder.embed([query]), dtype=np.float64)[0]
                if qv.ndim == 1 and qv.size == self._vectors.shape[1]:
                    query_vec = qv
            except Exception as e:
                logger.debug("查询向量化失败: %s", e)

        scored = []
        for rid, kind, text, metadata, created, use_count, last_used in rows:
            vec_score = 0.0
            idx = id_index.get(rid)
            if query_vec is not None and idx is not None:
                row_vec = self._vectors[idx]
                vec_score = float(np.dot(row_vec, query_vec))
            kw_score = _keyword_score(text, terms)
            raw = self.vector_weight * vec_score + (1 - self.vector_weight) * kw_score
            if raw <= 0:
                continue
            meta = _parse_meta(metadata)
            adjusted = _decay_score(
                raw, use_count, last_used, created,
                decay_days=self.decay_days,
                decay_factor=self.decay_factor,
                counter_example_penalty=self.counter_example_penalty,
                negative=bool(meta.get("negative")),
            )
            if adjusted <= 0:
                continue
            is_neg = 0 if not bool(meta.get("negative")) else 1
            scored.append((is_neg, -adjusted, rid,
                           _hit(kind, text, metadata, created, adjusted)))
        scored.sort()
        top = scored[:top_k]
        for _neg, _adj, rid, _h in top:
            self.bump(rid)
        return [h for _neg, _adj, _, h in top]

    def find_similar(self, text: str, top_k: int = 1,
                     kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """写入前去重：返回与 text 最相似的已有记忆（不做衰减与引用计数）。"""
        rows = self._conn.execute(
            "SELECT id, kind, text, metadata, created_at, use_count, last_used_at "
            "FROM memories ORDER BY id DESC"
        ).fetchall()
        if kinds is not None:
            rows = [r for r in rows if r[1] in kinds]
        if not rows:
            return []
        self._ensure_vectors()
        terms = [t for t in re.split(r"\W+", text.lower()) if len(t) > 1]
        id_index = {iid: i for i, iid in enumerate(self._ids)}
        query_vec: Optional[np.ndarray] = None
        if self._vectors is not None and self._vectors.shape[0] > 0:
            try:
                qv = np.asarray(self.embedder.embed([text]), dtype=np.float64)[0]
                if qv.ndim == 1 and qv.size == self._vectors.shape[1]:
                    query_vec = qv
            except Exception as e:
                logger.debug("去重查询向量化失败: %s", e)
        scored = []
        for rid, kind, rtext, metadata, created, _uc, _lu in rows:
            vec_score = 0.0
            idx = id_index.get(rid)
            if query_vec is not None and idx is not None:
                row_vec = self._vectors[idx]
                vec_score = float(np.dot(row_vec, query_vec))
            kw_score = _keyword_score(rtext, terms)
            score = self.vector_weight * vec_score + (1 - self.vector_weight) * kw_score
            if score <= 0:
                continue
            scored.append((score, rid, _hit(kind, rtext, metadata, created, score)))
        scored.sort(key=lambda x: -x[0])
        return [dict(h, id=rid) for _, rid, h in scored[:top_k]]

    def bump(self, memory_id: int) -> None:
        """引用/去重命中：use_count+1 并刷新 last_used_at。"""
        try:
            now = datetime.now().isoformat(timespec="seconds")
            self._conn.execute(
                "UPDATE memories SET use_count = use_count + 1, last_used_at = ? "
                "WHERE id = ?",
                (now, memory_id),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.warning("记忆引用计数更新失败: %s", e)
            return
        try:
            idx = self._ids.index(memory_id)
            self._use_counts[idx] += 1
            self._last_used[idx] = now
        except ValueError:
            pass

    def close(self) -> None:
        self._conn.close()

    def _trim(self) -> None:
        total = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        if total <= self.max_entities:
            return
        overflow = total - self.max_entities
        self._conn.execute("""
            DELETE FROM memories WHERE id IN (
                SELECT id FROM memories ORDER BY id ASC LIMIT ?
            )
        """, (overflow,))
        self._conn.commit()
        # 重建内存镜像
        self._load()


def _sanitize_chroma_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Chroma 元数据值清洗：丢弃 None 与空集合（chroma 1.5.x 拒绝空 list）。"""
    out: Dict[str, Any] = {}
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple, set, dict)) and len(v) == 0:
            continue
        if isinstance(v, (tuple, set)):
            v = list(v)
        out[k] = v
    return out


class ChromaMemoryStore(VectorMemoryStore):
    """Chroma 持久化向量后端（需 pip install chromadb）。"""

    def __init__(self, db_path: str = "memory.db",
                 collection: str = "alpha_swe_memories",
                 embedder: Optional[Embedder] = None,
                 decay_days: Optional[float] = None,
                 decay_factor: Optional[float] = None,
                 counter_example_penalty: Optional[float] = None):
        try:
            import chromadb
        except ImportError as e:
            raise RuntimeError("chroma 后端需要安装 chromadb") from e
        super().__init__(embedder or TfidfEmbedder())
        self.decay_days = decay_days if decay_days is not None else 30.0
        self.decay_factor = decay_factor if decay_factor is not None else 0.1
        self.counter_example_penalty = (
            counter_example_penalty if counter_example_penalty is not None else 0.3
        )
        self._client = chromadb.PersistentClient(
            path=vector_db_dir(db_path, "chroma"))
        self._dim = int(getattr(self.embedder, "dim", 0) or 0)
        self._collection = self._get_or_create_collection(collection)

    def _get_or_create_collection(self, name: str):
        """获取集合；嵌入维度与当前嵌入器不一致时自动重建。

        Chroma 集合的嵌入维度在创建时固定（由首次写入的向量决定）；当嵌入器配置
        变化（如 sentence-transformers 384 维 ↔ TF-IDF 8192 维）或回退切换时，
        旧集合会拒绝写入/查询并抛 InvalidArgumentError（"Collection expecting
        embedding with dimension of X, got Y"）。这里把当前嵌入器维度写入集合
        元数据（embed_dim），发现不一致时删除重建，保证后端永远可用。
        """
        existing = None
        try:
            existing = self._client.get_collection(name)
        except Exception:
            existing = None
        if existing is not None and self._dim:
            stored = (existing.metadata or {}).get("embed_dim")
            if stored is not None:
                mismatch = int(stored) != self._dim
            elif existing.count() == 0:
                mismatch = True  # 空集合无数据，直接重建为当前维度
            else:
                mismatch = not self._probe_dim(existing)
            if mismatch:
                logger.warning(
                    "集合 %s 嵌入维度与当前嵌入器 %s 不一致，重建集合（原数据被清除）",
                    name, self._dim,
                )
                try:
                    self._client.delete_collection(name)
                except Exception as e:
                    logger.warning("重建集合时删除旧集合失败: %s", e)
        col = self._client.get_or_create_collection(
            name, metadata={"hnsw:space": "cosine", "embed_dim": self._dim})
        try:
            cur = col.metadata or {}
            if cur.get("embed_dim") != self._dim:
                col.modify(metadata={**cur, "embed_dim": self._dim,
                                     "hnsw:space": "cosine"})
        except Exception as e:
            logger.debug("更新集合维度元数据失败: %s", e)
        return col

    def _probe_dim(self, collection) -> bool:
        """用 0 向量探测集合期望维度是否与当前嵌入器一致。"""
        try:
            collection.query(query_embeddings=[[0.0] * self._dim], n_results=1)
            return True
        except Exception:
            return False

    def remember(self, kind: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        meta = dict(metadata or {})
        meta["kind"] = kind
        meta.setdefault("use_count", 0)
        meta.setdefault("created_at", now)
        meta["last_used_at"] = now
        self._collection.upsert(
            ids=[uuid.uuid4().hex],
            documents=[text],
            metadatas=[_sanitize_chroma_metadata(meta)],
            embeddings=[self.embedder.embed([text])[0]],
        )

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self.search(query, top_k=top_k)

    def search(self, query: str, top_k: int = 5,
               kinds: Optional[List[str]] = None,
               metadata_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        clauses: List[Dict[str, Any]] = []
        if kinds:
            clauses.append({"kind": {"$in": list(kinds)}})
        if metadata_filter:
            clauses.append(dict(metadata_filter))
        if len(clauses) == 1:
            where = clauses[0]
        elif len(clauses) > 1:
            where = {"$and": clauses}
        else:
            where = None
        try:
            res = self._collection.query(
                query_embeddings=[self.embedder.embed([query])[0]],
                n_results=top_k,
                where=where,
            )
        except Exception as e:
            # 记忆检索失败不应影响 Agent 主流程：降级为空结果
            logger.warning("chroma 检索失败（降级为空结果）: %s", e)
            return []
        hits = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        adjusted = []
        for i, doc_id in enumerate(ids):
            meta = dict(metas[i] or {}) if i < len(metas) else {}
            kind = meta.pop("kind", "note")
            created = str(meta.pop("created_at", "") or "")
            last_used = str(meta.pop("last_used_at", "") or "")
            use_count = int(meta.get("use_count") or 0)
            raw = 1.0 - dists[i] if i < len(dists) else 0.0  # cosine -> 相似度
            adjusted_score = _decay_score(
                raw, use_count, last_used, created,
                decay_days=self.decay_days,
                decay_factor=self.decay_factor,
                counter_example_penalty=self.counter_example_penalty,
                negative=bool(meta.get("negative")),
            )
            hit = _hit(kind, docs[i], dict(meta), created, adjusted_score)
            hit["id"] = doc_id
            is_neg = 0 if not bool(meta.get("negative")) else 1
            adjusted.append((is_neg, -adjusted_score, hit))
        adjusted.sort()
        hits = [h for _neg, _adj, h in adjusted]
        for h in hits:
            self.bump(h.get("id"))
        return hits

    def find_similar(self, text: str, top_k: int = 1,
                     kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        where = None
        if kinds:
            where = {"kind": {"$in": kinds}}
        try:
            res = self._collection.query(
                query_embeddings=[self.embedder.embed([text])[0]],
                n_results=top_k,
                where=where,
            )
        except Exception as e:
            logger.warning("chroma find_similar 失败: %s", e)
            return []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        hits = []
        for i, doc_id in enumerate(ids):
            meta = dict(metas[i] or {}) if i < len(metas) else {}
            kind = meta.pop("kind", "note")
            created = str(meta.pop("created_at", "") or "")
            score = 1.0 - dists[i] if i < len(dists) else 0.0
            hits.append(dict(_hit(kind, docs[i], meta, created, score), id=doc_id))
        return hits

    def bump(self, memory_id: Any) -> None:
        """引用/去重命中：use_count+1 并刷新 last_used_at。"""
        try:
            got = self._collection.get(ids=[memory_id])
            metas = (got.get("metadatas") or [None] * 1) or [None]
            meta = dict(metas[0] or {})
            meta["use_count"] = int(meta.get("use_count") or 0) + 1
            meta["last_used_at"] = datetime.now().isoformat(timespec="seconds")
            self._collection.update(ids=[memory_id],
                                    metadatas=[_sanitize_chroma_metadata(meta)])
        except Exception as e:
            logger.warning("chroma bump 失败: %s", e)

    def close(self) -> None:
        # chromadb Client 在 Windows 上持有目录文件句柄，必须显式
        # 关闭才能释放；否则测试临时目录删除失败、磁盘持续堆积。
        try:
            self._client.close()
        except Exception as e:
            logger.warning("chroma 客户端关闭失败: %s", e)


class QdrantMemoryStore(VectorMemoryStore):
    """Qdrant 本地模式向量后端（需 pip install qdrant-client）。"""

    def __init__(self, db_path: str = "memory.db",
                 collection: str = "alpha_swe_memories",
                 embedder: Optional[Embedder] = None,
                 decay_days: Optional[float] = None,
                 decay_factor: Optional[float] = None,
                 counter_example_penalty: Optional[float] = None):
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import (PointStruct,
                                              VectorParams)
        except ImportError as e:
            raise RuntimeError("qdrant 后端需要安装 qdrant-client") from e
        super().__init__(embedder or TfidfEmbedder())
        self.decay_days = decay_days if decay_days is not None else 30.0
        self.decay_factor = decay_factor if decay_factor is not None else 0.1
        self.counter_example_penalty = (
            counter_example_penalty if counter_example_penalty is not None else 0.3
        )
        self._client = QdrantClient(path=vector_db_dir(db_path, "qdrant"))  # 本地模式
        self._collection = collection
        self._PointStruct = PointStruct
        existing = [c.name for c in self._client.get_collections().collections]
        if collection not in existing:
            self._client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(
                    size=self.embedder.dim, distance="Cosine"
                ),
            )

    def remember(self, kind: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        meta = dict(metadata or {})
        meta["kind"] = kind
        meta.setdefault("use_count", 0)
        meta.setdefault("created_at", now)
        meta["last_used_at"] = now
        self._client.upsert(
            collection_name=self._collection,
            points=[self._PointStruct(
                id=uuid.uuid4().int & ((1 << 63) - 1),
                vector=self.embedder.embed([text])[0],
                payload={"text": text, **meta},
            )],
        )

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self.search(query, top_k=top_k)

    def search(self, query: str, top_k: int = 5,
               kinds: Optional[List[str]] = None,
               metadata_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        from qdrant_client.models import (FieldCondition, Filter,
                                          MatchAny, MatchValue)
        must = []
        if kinds:
            must.append(FieldCondition(key="kind", match=MatchAny(any=kinds)))
        if metadata_filter:
            for key, value in metadata_filter.items():
                must.append(FieldCondition(key=key, match=MatchValue(value=value)))
        query_vector = self.embedder.embed([query])[0]
        if hasattr(self._client, "query_points"):  # qdrant-client >= 1.10
            res = self._client.query_points(
                collection_name=self._collection,
                query=query_vector,
                limit=top_k,
                query_filter=Filter(must=must) if must else None,
            )
            points = res.points
        else:  # 旧版本 API
            res = self._client.search(
                collection_name=self._collection,
                query_vector=query_vector,
                limit=top_k,
                query_filter=Filter(must=must) if must else None,
            )
            points = res
        adjusted = []
        for point in points:
            payload = dict(point.payload or {})
            text = payload.pop("text", "")
            kind = payload.pop("kind", "note")
            created = str(payload.pop("created_at", "") or "")
            last_used = str(payload.pop("last_used_at", "") or "")
            use_count = int(payload.get("use_count") or 0)
            adjusted_score = _decay_score(
                float(point.score), use_count, last_used, created,
                decay_days=self.decay_days,
                decay_factor=self.decay_factor,
                counter_example_penalty=self.counter_example_penalty,
                negative=bool(payload.get("negative")),
            )
            hit = _hit(kind, text, dict(payload), created, adjusted_score)
            hit["id"] = point.id
            is_neg = 0 if not bool(payload.get("negative")) else 1
            adjusted.append((is_neg, -adjusted_score, hit))
        adjusted.sort()
        hits = [h for _neg, _adj, h in adjusted]
        for h in hits:
            self.bump(h.get("id"))
        return hits

    def find_similar(self, text: str, top_k: int = 1,
                     kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        from qdrant_client.models import (FieldCondition, Filter,
                                          MatchAny)
        query_vector = self.embedder.embed([text])[0]
        f = Filter(must=[FieldCondition(key="kind", match=MatchAny(any=kinds))]) if kinds else None
        try:
            if hasattr(self._client, "query_points"):
                res = self._client.query_points(
                    collection_name=self._collection,
                    query=query_vector, limit=top_k, query_filter=f,
                )
                points = res.points
            else:
                res = self._client.search(
                    collection_name=self._collection,
                    query_vector=query_vector, limit=top_k, query_filter=f,
                )
                points = res
        except Exception as e:
            logger.warning("qdrant find_similar 失败: %s", e)
            return []
        hits = []
        for point in points:
            payload = dict(point.payload or {})
            text = payload.pop("text", "")
            kind = payload.pop("kind", "note")
            created = str(payload.pop("created_at", "") or "")
            hits.append(dict(_hit(kind, text, payload, created, float(point.score)),
                             id=point.id))
        return hits

    def bump(self, memory_id: Any) -> None:
        """引用/去重命中：use_count+1 并刷新 last_used_at。"""
        try:
            got = self._client.retrieve(
                collection_name=self._collection,
                ids=[memory_id], with_payload=True,
            )
            if not got:
                return
            payload = dict(got[0].payload or {})
            payload["use_count"] = int(payload.get("use_count") or 0) + 1
            payload["last_used_at"] = datetime.now().isoformat(timespec="seconds")
            self._client.set_payload(
                collection_name=self._collection,
                payload=payload,
                points=[memory_id],
            )
        except Exception as e:
            logger.warning("qdrant bump 失败: %s", e)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception as e:
            logger.warning("qdrant 客户端关闭失败: %s", e)


# ---- 内部工具 ----
def _hit(kind: str, text: str, metadata: Any, created_at: str,
         score: float = 0.0) -> Dict[str, Any]:
    meta = metadata
    if isinstance(metadata, str):
        try:
            meta = json.loads(metadata or "{}")
        except json.JSONDecodeError:
            meta = {}
    return {
        "kind": kind,
        "text": text,
        "metadata": meta or {},
        "created_at": created_at,
        "score": round(float(score), 4),
    }


def _parse_meta(raw_metadata: Any) -> Dict[str, Any]:
    """把 metadata（str 或 dict）规范化为 dict。"""
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if isinstance(raw_metadata, str):
        try:
            data = json.loads(raw_metadata or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _metadata_matches(raw_metadata: Any, wanted: Dict[str, Any]) -> bool:
    meta = raw_metadata
    if isinstance(raw_metadata, str):
        try:
            meta = json.loads(raw_metadata or "{}")
        except json.JSONDecodeError:
            meta = {}
    meta = meta or {}
    for key, value in wanted.items():
        if meta.get(key) != value:
            return False
    return True


def _keyword_score(text: str, terms: List[str]) -> float:
    if not terms:
        return 0.0
    lowered = text.lower()
    hits = sum(1 for t in terms if t in lowered)
    return hits / len(terms)

def format_experience_text(summary: Dict[str, Any]) -> str:
    """经验摘要的标准文本形式（与 remember_experience 写入内容一致）。"""
    return (
        f"任务: {summary.get('problem', '')}\n"
        f"步骤: {'; '.join(str(x) for x in summary.get('steps', [])[:10])}\n"
        f"解决: {summary.get('solution', '')}\n"
        f"结果: {summary.get('outcome', '')}"
    )


def classify_task_type(instruction: str) -> str:
    """从任务指令推断任务类型（fix / add / refactor / test / general）。"""
    text = str(instruction or "").lower()
    fix_kw = ["fix", "bug", "错误", "修复", "失败", "broken", "crash", "异常", "报错", "出错"]
    add_kw = ["add", "feature", "功能", "实现", "新增", "create", "添加", "端点", "endpoint", "接口"]
    refactor_kw = ["refactor", "重构", "优化", "clean", "整理", "重写", "rewrite"]
    test_kw = ["test", "测试", "用例", "pytest", "unit"]
    if any(k in text for k in fix_kw):
        return "fix"
    if any(k in text for k in add_kw):
        return "add"
    if any(k in text for k in refactor_kw):
        return "refactor"
    if any(k in text for k in test_kw):
        return "test"
    return "general"


def _ensure_columns(conn, table: str = "memories") -> None:
    """兼容旧库：确保 use_count / last_used_at 列存在。"""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if "use_count" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN use_count INTEGER DEFAULT 0")
    if "last_used_at" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN last_used_at TEXT")


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _decay_score(score: float, use_count: int, last_used_at: Optional[str],
                 created_at: Optional[str], decay_days: float = 30.0,
                 decay_factor: float = 0.1,
                 counter_example_penalty: float = 0.3,
                 negative: bool = False) -> float:
    """记忆可信度：引用越多越可信；长期未引用则衰减；反例降权。"""
    out = float(score)
    out *= 1.0 + 0.1 * min(int(use_count or 0), 5)  # 引用加成（最多 +50%）
    now = datetime.now()
    used = _parse_dt(last_used_at) or _parse_dt(created_at) or now
    days = max(0.0, (now - used).total_seconds() / 86400.0)
    if decay_days > 0 and days > decay_days:
        periods = (days - decay_days) / decay_days
        out *= decay_factor ** periods
    if negative:
        out *= 1.0 - counter_example_penalty
    return out