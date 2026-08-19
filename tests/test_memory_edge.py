# -*- coding: utf-8 -*-
"""记忆后端边界路径测试：helper 函数、Noop/SQLite/Hybrid 边缘、嵌入器回退。"""
import json
import os
import sqlite3
from unittest import mock

import numpy as np
import pytest

from agent.config import MemoryConfig
from agent.memory.embed import (OpenAIEmbedder, TfidfEmbedder,
                                build_embedder, find_local_model,
                                set_hf_offline_env)
from agent.memory.factory import (_derive_project_key, build_layered_memory,
                                  build_memory)
from agent.memory.store import (HybridLocalMemoryStore, NoopMemoryStore,
                                SqliteMemoryStore, _decay_score,
                                _ensure_columns, _hit, _keyword_score,
                                _metadata_matches, _parse_dt, _parse_meta,
                                classify_task_type, extract_symbols,
                                format_experience_text, vector_db_dir)


def test_vector_db_dir_derives_dir_from_file_path():
    assert vector_db_dir("memory.db", "chroma").endswith("memory.chroma")
    assert vector_db_dir("mem", "qdrant").endswith(
        os.path.join("mem", "qdrant"))


def test_extract_symbols_caps_and_dedup():
    syms = extract_symbols(
        "def a(): pass\ndef a(): pass\nclass b: pass\n"
        "function c(){}\nconst d=1\nlet e=2",
        max_symbols=3)
    assert syms == ["a", "b", "c"]


def test_noop_store_disabled(ws_tmp):
    store = NoopMemoryStore()
    assert store.disabled is True
    assert store.retrieve("x") == []
    assert store.search("x") == []
    assert store.find_similar("x") == []
    store.remember("note", "text")
    store.close()


def test_build_memory_none_backend(ws_tmp):
    store = build_memory(MemoryConfig(backend="none"))
    assert isinstance(store, NoopMemoryStore)


def test_build_memory_sqlite_failure_degrades(ws_tmp):
    # sqlite3.connect 到目录会抛错 -> 工厂降级为 NoopMemoryStore
    store = build_memory(MemoryConfig(backend="sqlite", db_path=str(ws_tmp)))
    assert isinstance(store, NoopMemoryStore)


def test_build_layered_memory_none(ws_tmp):
    store = build_layered_memory(
        MemoryConfig(backend="none", global_dir=str(ws_tmp / "global")))
    assert isinstance(store, NoopMemoryStore)


def test_derive_project_key_stable():
    k1 = _derive_project_key("/tmp/proj-a")
    k2 = _derive_project_key("/tmp/proj-a")
    assert k1 == k2 and k1.startswith("proj-a-")


def test_sqlite_search_filters_and_bump(ws_tmp):
    store = SqliteMemoryStore(db_path=str(ws_tmp / "m.db"))
    store.remember("note", "alpha keyword", metadata={"project": "p1"})
    store.remember("code", "beta keyword", metadata={"project": "p2"})
    hits = store.search("keyword", kinds=["code"],
                        metadata_filter={"project": "p2"})
    assert len(hits) == 1
    assert hits[0]["kind"] == "code"
    assert hits[0]["metadata"]["project"] == "p2"
    # search 内部对命中 bump：use_count 应已 +1
    conn = sqlite3.connect(str(ws_tmp / "m.db"))
    try:
        n = conn.execute(
            "SELECT use_count FROM memories WHERE kind='code'").fetchone()[0]
    finally:
        conn.close()
    assert n >= 1
    store.close()


def test_sqlite_find_similar_and_disabled(ws_tmp):
    store = SqliteMemoryStore(db_path=str(ws_tmp / "m2.db"))
    store.remember("note", "unique phrase xyz")
    sim = store.find_similar("unique phrase xyz", top_k=1)
    assert sim and "unique phrase xyz" in sim[0]["text"]
    assert store.disabled is False
    store.close()


def test_sqlite_trim(ws_tmp):
    store = SqliteMemoryStore(db_path=str(ws_tmp / "m3.db"),
                              max_entities=3)
    for i in range(6):
        store.remember("note", f"entry {i}")
    conn = sqlite3.connect(str(ws_tmp / "m3.db"))
    try:
        n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        conn.close()
    assert n <= 3
    store.close()


def test_hybrid_trim_rebuilds_mirror(ws_tmp):
    store = HybridLocalMemoryStore(
        db_path=str(ws_tmp / "h.db"), max_entities=3,
        embedder=TfidfEmbedder(max_features=16))
    for i in range(6):
        store.remember("note", f"hybrid entry {i}")
    store.close()
    # trim 契约：DB 行数不超过 max_entities
    conn = sqlite3.connect(str(ws_tmp / "h.db"))
    try:
        n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        conn.close()
    assert n <= 3
    # 重开后镜像从 DB 重建，检索仍可用
    store2 = HybridLocalMemoryStore(
        db_path=str(ws_tmp / "h.db"), max_entities=3,
        embedder=TfidfEmbedder(max_features=16))
    try:
        hits = store2.search("entry", top_k=3)
        assert len(hits) <= 3
    finally:
        store2.close()


def test_hybrid_find_similar_dedup(ws_tmp):
    store = HybridLocalMemoryStore(
        db_path=str(ws_tmp / "h2.db"),
        embedder=TfidfEmbedder(max_features=16))
    store.remember("note", "duplicate memory sentence here")
    sim = store.find_similar("duplicate memory sentence here", top_k=1)
    assert sim and sim[0]["id"] is not None
    store.close()


def test_hit_parses_string_metadata():
    h = _hit("note", "text", '{"a": 1}', "2026-01-01T00:00:00", 0.5)
    assert h["metadata"] == {"a": 1}
    assert h["score"] == 0.5
    h2 = _hit("note", "text", "{bad json", "t")
    assert h2["metadata"] == {}


def test_parse_meta_variants():
    assert _parse_meta({"a": 1}) == {"a": 1}
    assert _parse_meta('{"a": 1}') == {"a": 1}
    assert _parse_meta("not json") == {}
    assert _parse_meta(42) == {}
    assert _parse_meta('"str"') == {}


def test_metadata_matches():
    assert _metadata_matches('{"a": 1}', {"a": 1}) is True
    assert _metadata_matches({"a": 1}, {"a": 2}) is False
    assert _metadata_matches("{bad", {"a": 1}) is False
    assert _metadata_matches(None, {}) is True


def test_keyword_score():
    assert _keyword_score("alpha beta", ["alpha"]) == 1.0
    assert _keyword_score("alpha", ["alpha", "beta"]) == 0.5
    assert _keyword_score("text", []) == 0.0


def test_format_experience_text():
    out = format_experience_text({
        "problem": "p", "steps": ["s1", "s2"], "solution": "s",
        "outcome": "success"})
    assert "任务: p" in out and "s1; s2" in out and "结果: success" in out


def test_classify_task_type():
    assert classify_task_type("fix the bug") == "fix"
    assert classify_task_type("add a feature") == "add"
    assert classify_task_type("refactor the module") == "refactor"
    assert classify_task_type("write a unit test") == "test"
    assert classify_task_type("do something") == "general"
    assert classify_task_type("") == "general"


def test_ensure_columns_legacy_table(ws_tmp):
    conn = sqlite3.connect(str(ws_tmp / "legacy.db"))
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, "
                 "kind TEXT, text TEXT, metadata TEXT, created_at TEXT)")
    conn.commit()
    _ensure_columns(conn)
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(memories)").fetchall()}
    assert "use_count" in cols and "last_used_at" in cols
    conn.close()


def test_parse_dt():
    assert _parse_dt(None) is None
    assert _parse_dt("garbage") is None
    assert _parse_dt("2026-01-01T00:00:00") is not None


def test_decay_score():
    assert _decay_score(1.0, 5, None, None) == 1.5
    assert _decay_score(1.0, 0, None, None, negative=True) == 0.7
    old = _decay_score(1.0, 0, "2000-01-01T00:00:00", None,
                       decay_days=30, decay_factor=0.1)
    assert 0 < old < 1.0


def test_set_hf_offline_env(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    set_hf_offline_env()
    assert os.environ.get("HF_HUB_OFFLINE") == "1"


def test_find_local_model(tmp_path, monkeypatch):
    # 直接目录（含 modules.json）
    direct = tmp_path / "model"
    direct.mkdir()
    (direct / "modules.json").write_text("{}", encoding="utf-8")
    assert find_local_model("", str(direct)) == str(direct)
    # HF 缓存 snapshot 形态：设置 HF_HOME 后按 org/name 找到 snapshot
    hub = tmp_path / "hf"
    snap = hub / "hub" / "models--org--name" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    monkeypatch.setenv("HF_HOME", str(hub))
    assert find_local_model("org/name") == str(snap)


def test_find_local_model_missing_returns_none(ws_tmp, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(ws_tmp / "hf"))
    assert find_local_model("no-such-model-xyz", str(ws_tmp / "missing")) \
        is None


def test_openai_embedder_requires_key():
    with pytest.raises(ValueError):
        OpenAIEmbedder(api_key="")


def test_openai_embedder_mocked_urlopen():
    payload = json.dumps({
        "data": [{"embedding": [0.1, 0.2]},
                 {"embedding": [0.3, 0.4]}]}).encode("utf-8")
    emb = OpenAIEmbedder(api_key="test-key", dim=2)
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = payload
    with mock.patch("urllib.request.urlopen", return_value=resp):
        out = emb.embed(["a", "b"])
    assert len(out) == 2
    for v in out:
        assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-6


def test_build_embedder_openai_fallback(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    emb = build_embedder(MemoryConfig(embedder="openai"))
    assert isinstance(emb, TfidfEmbedder)


def test_build_embedder_tfidf():
    emb = build_embedder(MemoryConfig(embedder="tfidf"))
    assert isinstance(emb, TfidfEmbedder)
    assert emb.dim == 8192