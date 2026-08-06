"""记忆存储工厂 —— 按配置选择后端。

auto: chromadb > qdrant-client > 本地 Hybrid（TF-IDF + 关键词）。
"""
from __future__ import annotations

import logging
from typing import Optional

from agent.config import MemoryConfig
from agent.memory.embed import build_embedder
from agent.memory.store import (ChromaMemoryStore, HybridLocalMemoryStore,
                                MemoryStore, QdrantMemoryStore,
                                SqliteMemoryStore)

logger = logging.getLogger("alpha-swe.memory.factory")


def build_memory(config: Optional[MemoryConfig] = None) -> MemoryStore:
    """构造记忆存储后端。"""
    config = config or MemoryConfig()
    backend = config.backend

    if backend == "sqlite":
        return SqliteMemoryStore(db_path=config.db_path,
                                 max_entities=config.max_entities)

    if backend == "hybrid":
        return HybridLocalMemoryStore(
            db_path=config.db_path,
            max_entities=config.max_entities,
            embedder=build_embedder(config),
            vector_weight=config.hybrid_weight_vector,
            max_code_chars=config.max_code_index_chars,
        )

    if backend == "chroma":
        try:
            return ChromaMemoryStore(db_path=config.db_path,
                                     collection=config.collection,
                                     embedder=build_embedder(config))
        except Exception as e:
            logger.warning("chroma 后端不可用，回退 hybrid: %s", e)

    if backend == "qdrant":
        try:
            return QdrantMemoryStore(db_path=config.db_path,
                                     collection=config.collection,
                                     embedder=build_embedder(config))
        except Exception as e:
            logger.warning("qdrant 后端不可用，回退 hybrid: %s", e)

    # auto / 兜底
    for ctor, name in ((ChromaMemoryStore, "chroma"), (QdrantMemoryStore, "qdrant")):
        try:
            return ctor(db_path=config.db_path, collection=config.collection,
                        embedder=build_embedder(config))
        except Exception as e:
            logger.info("%s 后端不可用: %s", name, e)
    return HybridLocalMemoryStore(
        db_path=config.db_path,
        max_entities=config.max_entities,
        embedder=build_embedder(config),
        vector_weight=config.hybrid_weight_vector,
        max_code_chars=config.max_code_index_chars,
    )