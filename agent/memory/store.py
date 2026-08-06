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
from typing import Any, Dict, List, Optional

import numpy as np

from agent.memory.embed import Embedder, TfidfEmbedder

logger = logging.getLogger("alpha-swe.memory")

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
        try:
            self._client.close()
        except Exception as e:
            logger.warning("qdrant 客户端关闭失败: %s", e)

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
        text = (
            f"任务: {summary.get('problem', '')}\n"
            f"步骤: {'; '.join(str(s) for s in summary.get('steps', [])[:10])}\n"
            f"解决: {summary.get('solution', '')}\n"
            f"结果: {summary.get('outcome', '')}"
        )
        meta = {"key_files": summary.get("key_files", [])}
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
        self.remember("error", text, metadata or {})

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


class SqliteMemoryStore(MemoryStore):
    """SQLite 关键词检索（零依赖，兼容旧实现）。"""

    def __init__(self, db_path: str = "memory.db", max_entities: int = 1000):
        self.db_path = db_path
        self.max_entities = max_entities
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._conn.commit()

    def remember(self, kind: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        self._conn.execute(
            "INSERT INTO memories (kind, text, metadata) VALUES (?, ?, ?)",
            (kind, text, json.dumps(metadata or {}, ensure_ascii=False)),
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
            "SELECT kind, text, metadata, created_at FROM memories ORDER BY id DESC"
        ).fetchall()
        scored = []
        for kind, text, metadata, created in rows:
            if kinds is not None and kind not in kinds:
                continue
            if metadata_filter and not _metadata_matches(metadata, metadata_filter):
                continue
            score = sum(1 for t in terms if t in text.lower())
            if terms and score == 0:
                continue
            scored.append((score, _hit(kind, text, metadata, created)))
        scored.sort(key=lambda x: -x[0])
        return [item for _, item in scored[:top_k]]

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
                 max_code_chars: int = 2000):
        super().__init__(embedder)
        self.db_path = db_path
        self.max_entities = max_entities
        self.vector_weight = max(0.0, min(1.0, vector_weight))
        self.max_code_chars = max_code_chars
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._conn.commit()
        self._ids: List[int] = []
        self._texts: List[str] = []
        self._vectors: Optional[np.ndarray] = None
        self._dirty = False
        self._load()

    # ---- 内部 ----
    def _load(self) -> None:
        rows = self._conn.execute(
            "SELECT id, text FROM memories ORDER BY id"
        ).fetchall()
        self._ids = [r[0] for r in rows]
        self._texts = [r[1] for r in rows]
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
        self._conn.execute(
            "INSERT INTO memories (kind, text, metadata) VALUES (?, ?, ?)",
            (kind, text, json.dumps(metadata or {}, ensure_ascii=False)),
        )
        self._trim()
        self._conn.commit()
        row_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self._ids.append(row_id)
        self._texts.append(text)
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
            "SELECT id, kind, text, metadata, created_at FROM memories ORDER BY id DESC"
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
        for rid, kind, text, metadata, created in rows:
            vec_score = 0.0
            idx = id_index.get(rid)
            if query_vec is not None and idx is not None:
                row_vec = self._vectors[idx]
                vec_score = float(np.dot(row_vec, query_vec))
            kw_score = _keyword_score(text, terms)
            score = self.vector_weight * vec_score + (1 - self.vector_weight) * kw_score
            if score <= 0:
                continue
            scored.append((score, _hit(kind, text, metadata, created, score)))
        scored.sort(key=lambda x: -x[0])
        return [item for _, item in scored[:top_k]]

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


class ChromaMemoryStore(VectorMemoryStore):
    """Chroma 持久化向量后端（需 pip install chromadb）。"""

    def __init__(self, db_path: str = "memory.db",
                 collection: str = "alpha_swe_memories",
                 embedder: Optional[Embedder] = None):
        try:
            import chromadb
        except ImportError as e:
            raise RuntimeError("chroma 后端需要安装 chromadb") from e
        super().__init__(embedder or TfidfEmbedder())
        self._client = chromadb.PersistentClient(path=db_path)
        self._collection = self._client.get_or_create_collection(
            collection, metadata={"hnsw:space": "cosine"}
        )

    def remember(self, kind: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        meta = dict(metadata or {})
        meta["kind"] = kind
        self._collection.upsert(
            ids=[uuid.uuid4().hex],
            documents=[text],
            metadatas=[meta],
            embeddings=[self.embedder.embed([text])[0]],
        )

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self.search(query, top_k=top_k)

    def search(self, query: str, top_k: int = 5,
               kinds: Optional[List[str]] = None,
               metadata_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        where = None
        if kinds:
            where = {"kind": {"$in": kinds}}
        if metadata_filter:
            where = {**where, **metadata_filter} if where else dict(metadata_filter)
        res = self._collection.query(
            query_embeddings=[self.embedder.embed([query])[0]],
            n_results=top_k,
            where=where,
        )
        hits = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for i, doc_id in enumerate(ids):
            meta = dict(metas[i] or {}) if i < len(metas) else {}
            kind = meta.pop("kind", "note")
            score = 1.0 - dists[i] if i < len(dists) else 0.0  # cosine -> 相似度
            hits.append(_hit(kind, docs[i], meta, "", score))
        return hits

    def close(self) -> None:
        pass  # chromadb PersistentClient 无需显式关闭


class QdrantMemoryStore(VectorMemoryStore):
    """Qdrant 本地模式向量后端（需 pip install qdrant-client）。"""

    def __init__(self, db_path: str = "memory.db",
                 collection: str = "alpha_swe_memories",
                 embedder: Optional[Embedder] = None):
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import (Distance, PointStruct,
                                              VectorParams)
        except ImportError as e:
            raise RuntimeError("qdrant 后端需要安装 qdrant-client") from e
        super().__init__(embedder or TfidfEmbedder())
        self._client = QdrantClient(path=db_path)  # 本地模式
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
        meta = dict(metadata or {})
        meta["kind"] = kind
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
        hits = []
        for point in points:
            payload = dict(point.payload or {})
            text = payload.pop("text", "")
            kind = payload.pop("kind", "note")
            hits.append(_hit(kind, text, payload, "", float(point.score)))
        return hits

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