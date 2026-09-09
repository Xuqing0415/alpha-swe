# -*- coding: utf-8 -*-
"""主线一 1.3A/1.3B：访问台账 TTL/冷降权/清理提示 + 严格晋升标准。"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent.memory.layered import LayeredMemoryStore, _hash_text
from agent.memory.store import SqliteMemoryStore


def _hash(text):
    return _hash_text(text)


def _layered(ws_tmp, idx=0, project_key="proj-a", **kwargs):
    """照抄既有 test_memory_layered 的构造方式，并传入 global_meta_dir。"""
    pdir = ws_tmp / f"proj{idx}"
    pdir.mkdir(parents=True, exist_ok=True)
    gdir = ws_tmp / "global"
    gdir.mkdir(parents=True, exist_ok=True)
    return LayeredMemoryStore(
        project_store=SqliteMemoryStore(db_path=str(pdir / "memory.db")),
        global_store=SqliteMemoryStore(db_path=str(gdir / "memory.db")),
        project_key=project_key,
        global_meta_dir=str(gdir),
        **kwargs,
    )


def _seed_promotion(ws_tmp, key, entry):
    """直接写入 promotions.json，模拟前两个项目已应用的台账。"""
    gdir = ws_tmp / "global"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "promotions.json").write_text(
        json.dumps({key: entry}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_ttl_disabled_no_ledger_and_order_unchanged(ws_tmp):
    store = _layered(ws_tmp, 0, "proj-a")
    store.remember("note", "缓存穿透兜底方案记录B", {})
    store.remember("note", "缓存穿透兜底方案记录A", {})
    hits = store.search("缓存穿透 兜底")
    assert hits and hits[0]["text"] == "缓存穿透兜底方案记录B"
    assert store._ledger == {}
    assert not (ws_tmp / "global" / "access.json").exists()
    assert store.cleanup_candidates() == []
    assert store.note_session_start() == ""
    store.close()


def test_ttl_access_tracking_persists_across_reopen(ws_tmp):
    text = "缓存穿透回源加锁的经验记录"
    store = _layered(ws_tmp, 0, "proj-a", ttl_enabled=True)
    store.remember("note", text, {})
    hits = store.search("缓存穿透 回源")
    assert hits and hits[0]["layer"] == "project"
    store.close()

    key = _hash(text)
    ledger = _read_json(ws_tmp / "global" / "access.json")
    assert ledger[key]["access_count"] == 1
    assert ledger[key]["kind"] == "note"
    assert ledger[key]["layer"] == "project"

    reopened = _layered(ws_tmp, 0, "proj-a", ttl_enabled=True)
    assert reopened._ledger[key]["access_count"] == 1
    hits = reopened.search("缓存穿透 回源")
    assert hits and hits[0]["layer"] == "project"
    reopened.close()

    ledger = _read_json(ws_tmp / "global" / "access.json")
    assert ledger[key]["access_count"] == 2


def test_ttl_cold_hit_penalized_and_marked(ws_tmp):
    text = "缓存穿透 冷记忆 实验记录"
    store = _layered(ws_tmp, 0, "proj-a", ttl_enabled=True)
    store.remember("note", text, {})
    first = store.search("缓存穿透 冷记忆")[0]
    assert "cold" not in first

    key = _hash(text)
    store._ledger[key]["last_accessed"] = (
        datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
    stale = store.search("缓存穿透 冷记忆")[0]
    assert stale.get("cold") is True
    # 底层每次检索 +1 use_count（+10%），冷记忆再乘 cold_penalty=0.5
    assert stale["score"] == pytest.approx(first["score"] * 1.1 * 0.5)
    store.close()


def test_ttl_warm_hit_not_penalized(ws_tmp):
    text = "缓存穿透 冷记忆 实验记录"
    store = _layered(ws_tmp, 0, "proj-a", ttl_enabled=True)
    store.remember("note", text, {})
    store.search("缓存穿透 冷记忆")  # 首次访问，last_accessed = now
    warm = store.search("缓存穿透 冷记忆")[0]
    assert "cold" not in warm
    assert warm["score"] > 0
    store.close()


def test_ttl_same_score_hotter_memory_ranks_first(ws_tmp):
    b_text = "缓存穿透兜底方案记录B"
    a_text = "缓存穿透兜底方案记录A"
    store = _layered(ws_tmp, 0, "proj-a", ttl_enabled=True)
    store.remember("note", b_text, {})
    store.remember("note", a_text, {})
    # 同分且同热度：保持底层既有顺序（id 升序，B 在前）
    hits = store.search("缓存穿透 兜底")
    assert hits[0]["text"] == b_text
    # 抬高分热度后同分并列应排前
    store._ledger[_hash(a_text)]["access_count"] = 5
    hits = store.search("缓存穿透 兜底")
    assert hits[0]["text"] == a_text
    store.close()


def test_ttl_cleanup_candidates_and_session_note(ws_tmp):
    store = _layered(ws_tmp, 0, "proj-a", ttl_enabled=True)
    store.remember("note", "发布流程注意事项（旧）", {})
    store.search("发布流程")
    assert store.cleanup_candidates() == []
    assert store.note_session_start() == ""

    key = _hash("发布流程注意事项（旧）")
    store._ledger[key]["last_accessed"] = (
        datetime.now() - timedelta(days=95)).isoformat(timespec="seconds")
    candidates = store.cleanup_candidates()
    assert len(candidates) == 1
    assert candidates[0]["key"] == key
    assert candidates[0]["kind"] == "note"
    assert candidates[0]["days_idle"] >= 90
    assert "1 条项目记忆超过 90 天未访问" in store.note_session_start()
    store.close()


EXPERIENCE = "跨项目通用经验：失败后先复现再修，提交前跑全量测试"
QUERY = "失败后先复现再修"
OTHER_SAMPLE = "另一个项目里完全无关的部署排查脚本输出片段"


def test_strict_promotion_all_criteria_met_promotes(ws_tmp):
    stores = []
    for i in range(3):
        store = _layered(ws_tmp, i, f"proj-{i}", strict_promotion=True)
        store.remember("experience", EXPERIENCE,
                       {"task_type": "debug", "after_failure": True})
        store.search(QUERY)
        stores.append(store)
    assert stores[-1].global_store.search(QUERY), "满足全部条件应晋升全局层"

    entry = _read_json(ws_tmp / "global" / "promotions.json")[_hash(EXPERIENCE)]
    assert entry["promoted"] is True
    assert len(entry["apps"]) == 3
    status = stores[-1].promotion_readiness(EXPERIENCE)
    assert status["task_type_consistent"] is True
    assert status["min_similarity"] >= 0.6
    assert status["has_recovery"] is True
    assert status["ready"] is True
    assert status["missing"] == []
    for store in stores:
        store.close()


def test_strict_promotion_blocks_inconsistent_task_type(ws_tmp):
    key = _hash(EXPERIENCE)
    _seed_promotion(ws_tmp, key, {
        "projects": ["proj-0", "proj-1"],
        "promoted": False,
        "apps": [
            {"project": "proj-0", "task_type": "debug",
             "sample": EXPERIENCE[:80], "after_failure": True},
            {"project": "proj-1", "task_type": "add",
             "sample": OTHER_SAMPLE[:80], "after_failure": True},
        ],
    })
    store = _layered(ws_tmp, 2, "proj-2", strict_promotion=True)
    store.remember("experience", EXPERIENCE,
                   {"task_type": "debug", "after_failure": True})
    store.search(QUERY)
    assert store.global_store.search(QUERY) == [], "task_type 不一致不得晋升"
    status = store.promotion_readiness(EXPERIENCE)
    assert status["ready"] is False
    assert status["task_type_consistent"] is False
    assert any("任务类型" in r for r in status["missing"])
    entry = _read_json(ws_tmp / "global" / "promotions.json")[key]
    assert entry["promoted"] is False
    store.close()


def test_strict_promotion_blocks_low_context_similarity(ws_tmp):
    key = _hash(EXPERIENCE)
    _seed_promotion(ws_tmp, key, {
        "projects": ["proj-0", "proj-1"],
        "promoted": False,
        "apps": [
            {"project": "proj-0", "task_type": "debug",
             "sample": EXPERIENCE[:80], "after_failure": True},
            {"project": "proj-1", "task_type": "debug",
             "sample": OTHER_SAMPLE[:80], "after_failure": True},
        ],
    })
    store = _layered(ws_tmp, 2, "proj-2", strict_promotion=True)
    store.remember("experience", EXPERIENCE,
                   {"task_type": "debug", "after_failure": True})
    store.search(QUERY)
    assert store.global_store.search(QUERY) == [], "相似度不足不得晋升"
    status = store.promotion_readiness(EXPERIENCE)
    assert status["ready"] is False
    assert status["min_similarity"] < 0.6
    assert any("上下文相似度" in r for r in status["missing"])
    store.close()


def test_strict_promotion_blocks_without_failure_recovery(ws_tmp):
    key = _hash(EXPERIENCE)
    _seed_promotion(ws_tmp, key, {
        "projects": ["proj-0", "proj-1"],
        "promoted": False,
        "apps": [
            {"project": "proj-0", "task_type": "debug",
             "sample": EXPERIENCE[:80], "after_failure": False},
            {"project": "proj-1", "task_type": "debug",
             "sample": EXPERIENCE[:80], "after_failure": False},
        ],
    })
    store = _layered(ws_tmp, 2, "proj-2", strict_promotion=True)
    store.remember("experience", EXPERIENCE,
                   {"task_type": "debug", "after_failure": False})
    store.search(QUERY)
    assert store.global_store.search(QUERY) == [], "无恢复证据不得晋升"
    status = store.promotion_readiness(EXPERIENCE)
    assert status["ready"] is False
    assert status["has_recovery"] is False
    assert any("恢复证据" in r for r in status["missing"])
    store.close()


def test_strict_disabled_uses_old_promotion_logic(ws_tmp):
    # 同样的输入（task_type 不一致、无恢复证据）：strict 关闭时仍按旧逻辑晋升
    key = _hash(EXPERIENCE)
    _seed_promotion(ws_tmp, key, {
        "projects": ["proj-0", "proj-1"],
        "promoted": False,
        "apps": [
            {"project": "proj-0", "task_type": "debug",
             "sample": EXPERIENCE[:80], "after_failure": False},
            {"project": "proj-1", "task_type": "add",
             "sample": OTHER_SAMPLE[:80], "after_failure": False},
        ],
    })
    store = _layered(ws_tmp, 2, "proj-2")
    store.remember("experience", EXPERIENCE, {})
    store.search(QUERY)
    assert store.global_store.search(QUERY), "strict 关闭时旧逻辑应照常晋升"
    entry = _read_json(ws_tmp / "global" / "promotions.json")[key]
    assert entry["promoted"] is True
    store.close()
