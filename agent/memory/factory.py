"""记忆存储工厂 —— 按配置选择后端。

auto: chromadb > qdrant-client > 本地 Hybrid（TF-IDF + 关键词）。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

from agent.config import MemoryConfig
from agent.memory.embed import build_embedder
from agent.memory.store import (ChromaMemoryStore, HybridLocalMemoryStore,
                                MemoryStore, NoopMemoryStore,
                                QdrantMemoryStore, SqliteMemoryStore)

logger = logging.getLogger("alpha-swe.memory.factory")


def build_memory(config: Optional[MemoryConfig] = None) -> MemoryStore:
    """构造记忆存储后端。

    嵌入器只构建一次并在各后端尝试间复用：避免 auto 链路对 chroma/qdrant/hybrid
    逐个尝试时重复触发 sentence-transformers 加载（含失败重试）造成噪音与延迟。
    """
    config = config or MemoryConfig()
    _embedder_cache: list = []

    def _embedder():
        """惰性构建嵌入器（首次成功后复用，失败回退 TF-IDF 不再重试）。"""
        if not _embedder_cache:
            _embedder_cache.append(build_embedder(config))
        return _embedder_cache[0]

    backend = config.backend

    if backend in ("none", "off"):
        logger.info("长期记忆已禁用（backend=%s）", backend)
        return NoopMemoryStore()

    if backend == "sqlite":
        try:
            return SqliteMemoryStore(db_path=config.db_path,
                                     max_entities=config.max_entities,
                                     decay_days=config.decay_days,
                                     decay_factor=config.decay_factor,
                                     counter_example_penalty=config.counter_example_penalty)
        except Exception as e:
            logger.error("sqlite 后端不可用，降级到无记忆模式: %s", e)
            return NoopMemoryStore()

    if backend == "hybrid":
        return HybridLocalMemoryStore(
            db_path=config.db_path,
            max_entities=config.max_entities,
            embedder=_embedder(),
            vector_weight=config.hybrid_weight_vector,
            max_code_chars=config.max_code_index_chars,
            decay_days=config.decay_days,
            decay_factor=config.decay_factor,
            counter_example_penalty=config.counter_example_penalty,
        )

    if backend == "chroma":
        try:
            return ChromaMemoryStore(db_path=config.db_path,
                                     collection=config.collection,
                                     embedder=_embedder(),
                                     decay_days=config.decay_days,
                                     decay_factor=config.decay_factor,
                                     counter_example_penalty=config.counter_example_penalty)
        except Exception as e:
            logger.warning("chroma 后端不可用，回退 hybrid: %s", e)

    if backend == "qdrant":
        try:
            return QdrantMemoryStore(db_path=config.db_path,
                                     collection=config.collection,
                                     embedder=_embedder(),
                                     decay_days=config.decay_days,
                                     decay_factor=config.decay_factor,
                                     counter_example_penalty=config.counter_example_penalty)
        except Exception as e:
            logger.warning("qdrant 后端不可用，回退 hybrid: %s", e)

    # auto / 兜底
    try:
        for ctor, name in ((ChromaMemoryStore, "chroma"), (QdrantMemoryStore, "qdrant")):
            try:
                return ctor(db_path=config.db_path, collection=config.collection,
                            embedder=_embedder(),
                            decay_days=config.decay_days,
                            decay_factor=config.decay_factor,
                            counter_example_penalty=config.counter_example_penalty)
            except Exception as e:
                logger.info("%s 后端不可用: %s", name, e)
        return HybridLocalMemoryStore(
            db_path=config.db_path,
            max_entities=config.max_entities,
            embedder=_embedder(),
            vector_weight=config.hybrid_weight_vector,
            max_code_chars=config.max_code_index_chars,
            decay_days=config.decay_days,
            decay_factor=config.decay_factor,
            counter_example_penalty=config.counter_example_penalty,
        )
    except Exception as e:
        logger.error("记忆后端全部不可用，降级到无记忆模式: %s", e)
        return NoopMemoryStore()


def _derive_project_key(source: str) -> str:
    """从工作区路径推导项目标识（目录名 + 路径哈希），保持跨会话稳定。"""
    try:
        p = Path(source or ".").expanduser().resolve()
        name = p.name or "root"
        digest = hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:8]
        return f"{name}-{digest}"
    except Exception:
        return "unknown-project"


def build_layered_memory(config: Optional[MemoryConfig] = None,
                         project_key: str = "") -> MemoryStore:
    """构造三层记忆（会话 > 项目 > 全局，带跨项目晋升）。

    项目层使用项目内 db_path；全局层使用 ~/.swe-agent/memory/memory.db。
    backend=none 时整体降级为 NoopMemoryStore（保持既有禁用语义）。
    """
    from agent.memory.layered import LayeredMemoryStore

    config = config or MemoryConfig()
    if config.backend in ("none", "off"):
        logger.info("长期记忆已禁用（backend=%s），三层记忆一并关闭", config.backend)
        return NoopMemoryStore()

    try:
        Path(config.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    project_store = build_memory(config)
    global_dir = Path(config.global_dir).expanduser()
    try:
        global_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    global_cfg = config.model_copy(update={
        "db_path": str(global_dir / "memory.db"),
    })
    global_store = build_memory(global_cfg)
    if not project_key:
        project_key = _derive_project_key(config.db_path)
    return LayeredMemoryStore(
        project_store=project_store,
        global_store=global_store,
        project_key=project_key,
        global_meta_dir=str(global_dir),
        promotion_threshold=config.promotion_threshold,
    )
