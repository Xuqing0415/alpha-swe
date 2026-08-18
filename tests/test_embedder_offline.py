"""嵌入器离线与 httpx 生命周期修复测试。

覆盖：
- 无本地模型时 build_embedder 快速回退 TF-IDF（绝不联网、绝不抛异常）；
- 显式 sentence-transformers 配置同样回退（修复 TUI worker 崩溃）；
- find_local_model 的本地目录 / HF 缓存识别；
- 离线环境变量与 huggingface_hub 共享客户端重置。
"""

import pytest

from agent.config import MemoryConfig
from agent.memory.embed import (TfidfEmbedder, build_embedder, find_local_model,
                                reset_hf_http_session, set_hf_offline_env)


def test_build_embedder_explicit_st_falls_back_offline():
    """显式 sentence-transformers + 无本地模型 -> TF-IDF，且不触发网络/崩溃。"""
    cfg = MemoryConfig(embedder="sentence-transformers",
                       embedding_model="all-MiniLM-L6-v2",
                       embedding_model_path="__no_such_local_model__",
                       embedding_offline=True)
    emb = build_embedder(cfg)
    assert isinstance(emb, TfidfEmbedder)


def test_build_embedder_auto_falls_back_offline():
    cfg = MemoryConfig(embedder="auto",
                       embedding_model="all-MiniLM-L6-v2",
                       embedding_model_path="__no_such_local_model__",
                       embedding_offline=True)
    emb = build_embedder(cfg)
    assert isinstance(emb, TfidfEmbedder)


def test_build_embedder_missing_local_path_falls_back(ws_tmp):
    cfg = MemoryConfig(embedder="sentence-transformers",
                       embedding_model_path=str(ws_tmp / "no-such-model"),
                       embedding_offline=True)
    emb = build_embedder(cfg)
    assert isinstance(emb, TfidfEmbedder)


def test_find_local_model_detects_flat_dir(ws_tmp):
    model_dir = ws_tmp / "mymodel"
    model_dir.mkdir(parents=True)
    (model_dir / "modules.json").write_text("{}", encoding="utf-8")
    assert find_local_model("whatever", str(model_dir)) == str(model_dir)


def test_find_local_model_detects_hf_cache_snapshot(ws_tmp, monkeypatch):
    hub = ws_tmp / "hf_hub"
    repo = hub / "hub" / "models--sentence-transformers--all-MiniLM-L6-v2"
    snap = repo / "snapshots" / "abcdef123456"
    snap.mkdir(parents=True)
    (snap / "modules.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HF_HOME", str(hub))
    found = find_local_model("all-MiniLM-L6-v2")
    assert found == str(snap)


def test_find_local_model_missing_returns_none(ws_tmp, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(ws_tmp / "empty_hf"))
    assert find_local_model("all-MiniLM-L6-v2") is None
    # 指定不存在的路径返回 None
    assert find_local_model("x", str(ws_tmp / "nope")) is None


def test_hf_offline_env_idempotent():
    set_hf_offline_env()
    import os
    assert os.environ.get("HF_HUB_OFFLINE") == "1"
    assert os.environ.get("TRANSFORMERS_OFFLINE") == "1"
    set_hf_offline_env()  # 幂等，不覆盖用户显式值
    assert os.environ.get("HF_HUB_OFFLINE") == "1"


def test_reset_hf_http_session_no_crash():
    """huggingface_hub 已安装时重置共享客户端不抛异常（生命周期根源修复）。"""
    import importlib.util
    if importlib.util.find_spec("huggingface_hub") is None:
        pytest.skip("huggingface_hub 未安装")
    reset_hf_http_session()
    reset_hf_http_session()  # 连续调用也应安全
