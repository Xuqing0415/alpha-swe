# -*- coding: utf-8 -*-
"""阶段一：长期记忆闭环测试 —— 去重、反例降权、任务类型过滤、可信度衰减、A/A' 复用。"""
import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.memory.factory import build_memory
from agent.memory.store import (HybridLocalMemoryStore, _decay_score,
                                 format_experience_text)


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


class ClosedLoopLLM(MockLLM):
    """主循环响应 + 固定经验摘要响应。"""

    def __init__(self, main, experience):
        super().__init__()
        self._main = main
        self._experience = experience

    async def complete(self, messages):
        system = messages[0].get("content", "") if messages else ""
        if "经验总结器" in system:
            return self._experience
        return self._main


def make_config(ws_tmp, **mem_kw):
    return AppConfig(
        agent=AgentConfig(max_rounds=5, max_retries=2),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"), **mem_kw),
        mcp=MCPOptions(enabled=False),
    )


EXP = ('{"problem": "修复登录模块超时", "steps": ["定位超时点", "加超时处理"], '
       '"solution": "设置 5s 超时并重试", "outcome": "success", "key_files": []}')


# ---- 去重与引用计数 ----

def test_hybrid_find_similar_returns_id_and_bump(ws_tmp):
    store = HybridLocalMemoryStore(db_path=str(ws_tmp / "m.db"))
    store.remember_experience({"problem": "CI 超时", "steps": ["分析日志"],
                               "solution": "缓存依赖", "outcome": "success",
                               "key_files": []})
    similar = store.find_similar("CI 超时 缓存", top_k=1, kinds=["experience"])
    assert similar and similar[0]["id"] is not None
    assert similar[0]["kind"] == "experience"
    row = store._conn.execute(
        "SELECT use_count FROM memories WHERE id=?", (similar[0]["id"],)).fetchone()
    assert row[0] == 0
    store.bump(similar[0]["id"])
    row = store._conn.execute(
        "SELECT use_count FROM memories WHERE id=?", (similar[0]["id"],)).fetchone()
    assert row[0] == 1
    store.close()


def test_dedup_threshold_skips_near_duplicate(ws_tmp):
    store = HybridLocalMemoryStore(db_path=str(ws_tmp / "m.db"))
    exp = {"problem": "数据库连接泄漏", "steps": ["复现"],
           "solution": "使用连接池", "outcome": "success", "key_files": []}
    store.remember_experience(exp)
    n_before = store._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    # 去重查询应使用完整经验文本（与写入内容一致），相似度才接近 1
    similar = store.find_similar(format_experience_text(exp),
                                 top_k=1, kinds=["experience"])
    assert float(similar[0]["score"]) > 0.95  # 近乎重复
    # 模拟去重路径：命中则不写入，只 bump
    store.bump(similar[0]["id"])
    n_after = store._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert n_before == n_after
    store.close()


# ---- 反例降权 ----

def test_counter_example_penalty_ranks_positive_first(ws_tmp):
    store = HybridLocalMemoryStore(db_path=str(ws_tmp / "m.db"))
    store.remember_experience({"problem": "数据库连接泄漏",
                               "solution": "使用连接池", "outcome": "success"})
    store.remember_error("ConnectionError", "连接池未释放导致泄漏",
                         solution="未解决")
    hits = store.search("数据库连接泄漏 连接池", top_k=5)
    assert hits
    assert hits[0]["kind"] == "experience"  # 正例优先于反例
    assert any(h["kind"] == "error" for h in hits)  # 反例降权但不丢失
    store.close()


# ---- 任务类型过滤 ----

def test_task_type_metadata_filter(ws_tmp):
    store = HybridLocalMemoryStore(db_path=str(ws_tmp / "m.db"))
    store.remember_experience({"problem": "修复登录 bug",
                               "solution": "修校验", "outcome": "success"})
    store.remember_experience({"problem": "新增用户端点",
                               "solution": "加路由", "outcome": "success"})
    hits = store.search("用户 端点", kinds=["experience"],
                        metadata_filter={"task_type": "add"})
    assert hits and all(h["metadata"]["task_type"] == "add" for h in hits)
    assert any("新增用户端点" in h["text"] for h in hits)
    store.close()


# ---- 可信度衰减 ----

def test_decay_score_math():
    fresh = _decay_score(1.0, use_count=1, last_used_at=None, created_at=None,
                         decay_days=30, decay_factor=0.1)
    stale = _decay_score(1.0, use_count=1,
                         last_used_at="2000-01-01T00:00:00",
                         created_at="2000-01-01T00:00:00",
                         decay_days=30, decay_factor=0.1)
    assert stale < fresh
    neg = _decay_score(1.0, use_count=0, last_used_at=None, created_at=None,
                       counter_example_penalty=0.3, negative=True)
    assert neg == pytest.approx(0.7)
    bumped = _decay_score(1.0, use_count=5, last_used_at=None, created_at=None)
    assert bumped == pytest.approx(1.5)  # 引用加成最多 +50%


# ---- A/A' 闭环（loop 级） ----

@pytest.mark.asyncio
async def test_closed_loop_aa_retrieves_and_dedups(ws_tmp):
    cfg_a = make_config(ws_tmp, backend="hybrid")
    cfg_b = make_config(ws_tmp, backend="hybrid")
    store = build_memory(cfg_a.memory)  # 两个 loop 共享同一后端
    try:
        loop_a = AgentLoop(config=cfg_a, llm=ClosedLoopLLM('{"final_answer": "完成"}', EXP),
                           planner=StubPlanner(), memory=store)
        await loop_a.run("修复登录模块的 bug")
        # A 完成后：写入经验 + 检索决策
        names_a = {d["name"] for d in loop_a._decision.records()}
        assert "memory.write" in names_a
        assert "memory.retrieve" in names_a

        # A'（相似任务）：应复用 A 的经验并触发去重
        loop_b = AgentLoop(config=cfg_b, llm=ClosedLoopLLM('{"final_answer": "完成"}', EXP),
                           planner=StubPlanner(), memory=store)
        await loop_b.run("修复登录模块的 bug（同样超时）")
        names_b = {d["name"] for d in loop_b._decision.records()}
        assert "memory.retrieve" in names_b
        assert "memory.dedup" in names_b, "相似经验应触发去重而非重复写入"

        # 库中经验只有一条（去重未重复存储）
        total = store._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE kind='experience'").fetchone()[0]
        assert total == 1
    finally:
        store.close()


def test_vector_backend_dedup_and_bump(ws_tmp):
    """qdrant/chroma（若可用）也应具备去重、引用计数与反例降权闭环。"""
    from agent.config import MemoryConfig
    from agent.memory.factory import build_memory
    store = None
    backend = None
    for b in ("qdrant", "chroma"):
        try:
            store = build_memory(MemoryConfig(backend=b, db_path=str(ws_tmp / f"{b}.db")))
            backend = b
            break
        except Exception:
            continue
    if store is None:
        pytest.skip("qdrant/chroma 后端不可用")
    try:
        exp = {"problem": "内存泄漏修复", "solution": "加清理",
               "outcome": "success", "key_files": []}
        store.remember_experience(exp)
        sim = store.find_similar(format_experience_text(exp),
                                 top_k=1, kinds=["experience"])
        assert sim, f"{backend} find_similar 应命中自身"
        assert sim[0].get("id") is not None
        assert float(sim[0]["score"]) > 0.9
        store.bump(sim[0]["id"])
        # 正例优先于反例
        store.remember_error("MemoryError", "内存泄漏未清理")
        hits = store.search("内存泄漏 清理", top_k=5)
        assert hits and hits[0]["kind"] == "experience"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_error_memory_marked_negative(ws_tmp):
    cfg = make_config(ws_tmp)
    store = build_memory(cfg.memory)
    try:
        loop = AgentLoop(config=cfg, llm=ClosedLoopLLM('{"not": "valid"}', EXP),
                         planner=StubPlanner(), memory=store)
        result = await loop.run("会失败的任务")
        assert result.ok is False
        hits = store.search("输出解析失败 超过最大轮数", kinds=["error"])
        assert hits and hits[0]["metadata"].get("negative") is True
        assert any(d["name"] == "memory.write" and "反例" in d["decision"]
                   for d in loop._decision.records())
    finally:
        store.close()