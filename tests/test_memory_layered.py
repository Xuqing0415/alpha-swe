# -*- coding: utf-8 -*-
"""主线一 1.3：项目级记忆分层（会话 > 项目 > 全局 + 跨项目晋升）。"""
import json

from agent.config import MemoryConfig
from agent.memory.factory import build_layered_memory
from agent.memory.layered import LayeredMemoryStore, SessionMemoryStore
from agent.memory.store import NoopMemoryStore, SqliteMemoryStore


def _layered(ws_tmp, project_key="proj-a"):
    return LayeredMemoryStore(
        project_store=SqliteMemoryStore(db_path=str(ws_tmp / "p.db")),
        global_store=SqliteMemoryStore(db_path=str(ws_tmp / "g.db")),
        project_key=project_key,
    )


def test_layered_search_priority_session_over_project(ws_tmp):
    store = _layered(ws_tmp)
    store.remember("note", "登录模块超时处理方案（项目历史）", {})
    store.remember("note", "登录模块超时处理方案（会话内确认）", {"layer": "session"})
    hits = store.search("登录模块 超时")
    assert hits, "应检索到结果"
    assert hits[0]["layer"] == "session", "会话层优先于项目层"
    layers = [h.get("layer") for h in hits]
    assert "project" in layers, "项目层结果应同时出现"
    store.close()


def test_layered_write_routing_by_layer_metadata(ws_tmp):
    store = _layered(ws_tmp)
    store.remember("note", "默认路由到项目层", {})
    store.remember("note", "显式路由到会话层", {"layer": "session"})
    store.remember("note", "显式路由到全局层", {"layer": "global"})
    assert store.search("默认路由到项目层")[0]["layer"] == "project"
    assert store.search("显式路由到会话层")[0]["layer"] == "session"
    assert store.search("显式路由到全局层")[0]["layer"] == "global"
    store.close()


def test_layered_zero_score_session_does_not_shadow_project(ws_tmp):
    store = _layered(ws_tmp)
    store.remember("note", "无关的临时笔记", {"layer": "session"})
    store.remember("note", "目标项目记忆", {})
    hits = store.search("目标项目记忆")
    assert hits and hits[0]["layer"] == "project", "score=0 的会话项不应抢占项目精确匹配"
    store.close()


def test_layered_format_context_has_layer_prefix(ws_tmp):
    store = _layered(ws_tmp)
    store.remember("experience", "项目特有约定", {})
    store.remember("experience", "全局通用经验", {"layer": "global"})
    assert "[项目][experience]" in store.format_context(store.search("约定"))
    assert "[全局][experience]" in store.format_context(store.search("通用经验"))
    store.close()


def test_promotion_after_three_projects(ws_tmp):
    global_dir = ws_tmp / "global"
    text = "跨项目通用经验：提交前先跑全量测试"
    stores = []
    for i in range(3):
        cfg = MemoryConfig(backend="sqlite", db_path=str(ws_tmp / f"proj{i}" / "memory.db"))
        cfg.global_dir = str(global_dir)
        s = build_layered_memory(cfg, project_key=f"proj-{i}")
        s.remember("experience", text, {})
        hits = s.search("提交前先跑全量测试")
        assert hits and hits[0]["layer"] == "project"
        stores.append(s)
    for s in stores:
        s.close()
    promo = json.loads((global_dir / "promotions.json").read_text(encoding="utf-8"))
    assert any(e.get("promoted") for e in promo.values()), "达到阈值后经验应晋升全局层"
    # 全局库中应包含晋升后的经验
    gcfg = MemoryConfig(backend="sqlite", db_path=str(global_dir / "memory.db"))
    probe = build_layered_memory(gcfg, project_key="probe")
    hits = probe.search("提交前先跑全量测试")
    assert any("promoted" in (h.get("metadata") or {}) for h in hits)
    probe.close()


def test_layered_backend_none_disables_all(ws_tmp):
    cfg = MemoryConfig(backend="none", db_path=str(ws_tmp / "x.db"))
    store = build_layered_memory(cfg, project_key="proj-a")
    assert isinstance(store, NoopMemoryStore)
    assert store.disabled is True


def test_layered_session_store_basic(ws_tmp):
    store = SessionMemoryStore()
    store.remember("note", "会话临时信息", {})
    assert store.search("会话临时信息")[0]["layer"] == "session"
    store.close()
    assert store.search("会话临时信息") == []
