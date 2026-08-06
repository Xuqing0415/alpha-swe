"""向量记忆测试：Hybrid 本地后端 + 可插拔后端（存在时）。"""
import importlib

import numpy as np
import pytest

from agent.config import MemoryConfig
from agent.memory.embed import TfidfEmbedder
from agent.memory.factory import build_memory
from agent.memory.store import (HybridLocalMemoryStore, MemoryStore,
                                SqliteMemoryStore, extract_symbols)


def make_store(ws_tmp, backend="hybrid", **kw):
    cfg = MemoryConfig(backend=backend, db_path=str(ws_tmp / "mem.db"), **kw)
    return build_memory(cfg)


def test_tfidf_embedder_normalized():
    emb = TfidfEmbedder()
    vecs = emb.embed(["如何修复内存泄漏", "python memory leak fix"])
    assert len(vecs) == 2 and len(vecs[0]) > 0
    for v in vecs:
        assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-6


def test_hybrid_relevance_ranking(ws_tmp):
    store = make_store(ws_tmp)
    store.remember("experience", "修复内存泄漏: 检查循环引用并释放不再使用的连接")
    store.remember("note", "今天天气不错，适合散步")
    hits = store.retrieve("内存泄漏 修复", top_k=2)
    assert hits[0]["text"].startswith("修复内存泄漏")


def test_kind_filter(ws_tmp):
    store = make_store(ws_tmp)
    store.remember("experience", "写测试: 使用 pytest 参数化")
    store.index_code("src/app.py", "def helper(): pass\nclass Widget: pass")
    code_hits = store.search("Widget helper", kinds=["code"])
    assert code_hits
    assert all(h["kind"] == "code" for h in code_hits)
    assert "Widget" in code_hits[0]["text"]


def test_index_code_symbols_and_path(ws_tmp):
    store = make_store(ws_tmp)
    store.index_code("src/app.py", "def add(a,b):\n    return a+b\nclass Calc: pass")
    hits = store.search("Calc add", kinds=["code"])
    assert hits and hits[0]["metadata"]["path"] == "src/app.py"
    assert "add" in hits[0]["metadata"]["symbols"]


def test_error_memory(ws_tmp):
    store = make_store(ws_tmp)
    store.remember_error("ValueError", "convert failed", solution="加 try/except")
    hits = store.search("ValueError 转换失败")
    assert hits and hits[0]["kind"] == "error"
    assert "加 try/except" in hits[0]["text"]


def test_experience_memory(ws_tmp):
    store = make_store(ws_tmp)
    store.remember_experience({"problem": "CI 超时", "steps": ["分析日志", "缓存依赖"],
                               "solution": "启用缓存", "outcome": "success"})
    hits = store.search("CI 超时 缓存")
    assert hits and hits[0]["kind"] == "experience"
    assert "启用缓存" in hits[0]["text"]


def test_persistence_across_reopen(ws_tmp):
    path = str(ws_tmp / "mem.db")
    store = HybridLocalMemoryStore(db_path=path)
    store.remember("note", "关键知识: 配置项 x 需要重启生效")
    store.close()
    store2 = HybridLocalMemoryStore(db_path=path)
    hits = store2.search("配置项 x")
    assert hits and "重启生效" in hits[0]["text"]
    store2.close()


def test_sqlite_backend_still_works(ws_tmp):
    store = SqliteMemoryStore(db_path=str(ws_tmp / "sqlite.db"))
    store.remember("note", "sqlite 关键词记忆")
    assert store.retrieve("关键词记忆")[0]["kind"] == "note"
    store.close()


def test_auto_backend_selection(ws_tmp):
    # auto: 有 qdrant/chroma 用之，否则回退 HybridLocal；行为应一致
    store = make_store(ws_tmp, backend="auto")
    assert isinstance(store, MemoryStore)
    store.remember("note", "auto 后端可用")
    assert store.search("auto 后端")[0]["kind"] == "note"
    store.close()


def test_hybrid_backend_forced(ws_tmp):
    store = make_store(ws_tmp, backend="hybrid")
    assert isinstance(store, HybridLocalMemoryStore)
    store.remember("note", "hybrid 强制后端")
    assert store.search("hybrid 强制")[0]["kind"] == "note"
    store.close()


def test_extract_symbols():
    syms = extract_symbols(
        "def foo(): pass\nclass Bar:\n    def baz(self): pass\nconst x = 1"
    )
    assert "foo" in syms and "Bar" in syms and "baz" in syms


@pytest.mark.skipif(importlib.util.find_spec("chromadb") is None,
                    reason="chromadb 未安装")
def test_chroma_backend(ws_tmp):
    from agent.memory.store import ChromaMemoryStore

    store = ChromaMemoryStore(db_path=str(ws_tmp / "chroma"), collection="test_store")
    store.remember("note", "chroma 向量记忆")
    hits = store.search("chroma 向量")
    assert hits and hits[0]["kind"] == "note"


@pytest.mark.skipif(importlib.util.find_spec("qdrant_client") is None,
                    reason="qdrant-client 未安装")
def test_qdrant_backend(ws_tmp):
    from agent.memory.store import QdrantMemoryStore

    store = QdrantMemoryStore(db_path=str(ws_tmp / "qdrant"), collection="test_store")
    store.remember("note", "qdrant 向量记忆")
    hits = store.search("qdrant 向量")
    assert hits and hits[0]["kind"] == "note"
    store.close()