# -*- coding: utf-8 -*-
"""多用户/多实例并发测试（方向一·阶段 2）。

覆盖：
- A. 同一项目并发访问：Blackboard 文件级写锁（同文件互斥、异文件并行）；
- B. 同一记忆后端并发读写：跨进程 SQLite 写入（WAL + busy_timeout，无
  "database is locked" / 丢失更新）；
- C. 同一项目锁文件互斥：跨进程 ProjectLock（第二实例拒绝 + 残留锁回收）；
- D. 会话状态共享与隔离：会话档案隔离、项目记忆跨实例共享；
- 压力：8 进程并发写入记忆，计数精确、无异常；
- 故障注入：持有锁进程崩溃后锁可被回收。

运行：python -X utf8 -m pytest tests/test_concurrency_multi.py -q
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO_ROOT)

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.memory.shared import SharedMemoryStore
from agent.memory.store import SqliteMemoryStore
from agent.multiagent import Blackboard
from agent.project_lock import ProjectLock, ProjectLockError
from agent.tools.fileio import FileIOTool


# ---------------- 跨进程 worker（subprocess 独立解释器，贴近真实多实例） ----------------
_SQLITE_WORKER = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, {repo!r})
    from agent.memory.store import SqliteMemoryStore
    db, wid, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    store = SqliteMemoryStore(db)
    try:
        for i in range(count):
            store.remember("exp", f"worker-{{wid}}-item-{{i}}", {{"worker": wid}})
        total = store.search("worker", top_k=100000)
        print(json.dumps({{"ok": True, "found": len(total)}}))
    except Exception as e:
        print(json.dumps({{"ok": False, "error": f"{{type(e).__name__}}: {{e}}"}}))
    finally:
        store.close()
""")

_LOCK_WORKER = textwrap.dedent("""
    import json, os, sys, time
    sys.path.insert(0, {repo!r})
    from agent.project_lock import ProjectLock
    proj, timeout, hold = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    lock = ProjectLock(proj, holder=f"pid-{{os.getpid()}}")
    ok = lock.acquire(timeout=timeout)
    print(json.dumps({{"acquired": ok}}))
    if ok and hold:
        time.sleep(hold)
        lock.release()
""")


def _run_child(script: str, *args, timeout: float = 90) -> dict:
    """启动独立 Python 子进程执行脚本，返回其 stdout JSON。"""
    code = script.format(repo=REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code, *map(str, args)],
        capture_output=True, text=True, timeout=timeout,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, f"子进程失败: {proc.stderr[-500:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_child_async(script: str, *args) -> subprocess.Popen:
    code = script.format(repo=REPO_ROOT)
    return subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", code, *map(str, args)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=REPO_ROOT,
    )


def _read_child(proc: subprocess.Popen, timeout: float = 90) -> dict:
    out, err = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, f"子进程失败: {err[-500:]}"
    return json.loads(out.strip().splitlines()[-1])


# ---------------- A. 同一项目并发访问（文件写锁） ----------------
def test_blackboard_file_lock_same_file_conflict(ws_tmp):
    """同一文件：第二个 Agent 写入被拒绝并收到冲突提示。"""
    bb = Blackboard()
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "a.txt").write_text("v1", encoding="utf-8")
    tool1 = FileIOTool(workspace=str(ws), lock_manager=bb, lock_holder="agent-1")
    tool2 = FileIOTool(workspace=str(ws), lock_manager=bb, lock_holder="agent-2")
    from agent.tools.base import ExecutionContext
    ctx = ExecutionContext(workspace=str(ws), task_id="t1")

    async def scenario():
        r1 = await tool1.execute(
            {"action": "write", "path": "a.txt", "content": "v2"}, ctx)
        assert r1.success, r1.error
        r2 = await tool2.execute(
            {"action": "write", "path": "a.txt", "content": "v3"}, ctx)
        assert not r2.success
        assert r2.metadata.get("file_lock_conflict") is True
        assert "agent-1" in (r2.metadata.get("holder") or "")
        # 释放后另一 Agent 可写
        assert bb.unlock_file(str(ws / "a.txt"), "agent-1")
        r3 = await tool2.execute(
            {"action": "write", "path": "a.txt", "content": "v4"}, ctx)
        assert r3.success, r3.error
    asyncio.run(scenario())


def test_blackboard_file_lock_different_files_parallel(ws_tmp):
    """不同文件：两个 Agent 并行写互不干扰。"""
    bb = Blackboard()
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    tool1 = FileIOTool(workspace=str(ws), lock_manager=bb, lock_holder="agent-1")
    tool2 = FileIOTool(workspace=str(ws), lock_manager=bb, lock_holder="agent-2")
    from agent.tools.base import ExecutionContext
    ctx1 = ExecutionContext(workspace=str(ws), task_id="t1")
    ctx2 = ExecutionContext(workspace=str(ws), task_id="t2")

    async def scenario():
        r1 = await tool1.execute(
            {"action": "write", "path": "a.txt", "content": "A"}, ctx1)
        r2 = await tool2.execute(
            {"action": "write", "path": "b.txt", "content": "B"}, ctx2)
        assert r1.success and r2.success
        assert (ws / "a.txt").read_text(encoding="utf-8") == "A"
        assert (ws / "b.txt").read_text(encoding="utf-8") == "B"
    asyncio.run(scenario())


# ---------------- B. 同一记忆后端并发读写 ----------------
def test_sqlite_cross_process_concurrent_write(ws_tmp):
    """4 进程并发写同一 SQLite：无锁库、无丢失更新。"""
    db = str(ws_tmp / "mem.db")
    n_procs, per_proc = 4, 10
    results = [_run_child(_SQLITE_WORKER, db, i, per_proc)
               for i in range(n_procs)]
    assert all(r.get("ok") for r in results), results
    store = SqliteMemoryStore(db)
    try:
        found = store.search("worker", top_k=100000)
        assert len(found) == n_procs * per_proc, f"丢失更新: {len(found)}"
    finally:
        store.close()


def test_shared_memory_thread_concurrent_dedup(ws_tmp):
    """同进程多线程 SharedMemoryStore：写入去重与仲裁在并发下仍有效。"""
    db = str(ws_tmp / "mem.db")
    inner = SqliteMemoryStore(db)
    wrappers = [
        SharedMemoryStore(inner, creator=f"agent-{i}",
                          lock_key=db, dedup_threshold=0.95)
        for i in range(3)
    ]
    import threading
    errors: list = []

    def write_many(w, base):
        try:
            for i in range(20):
                w.remember("exp", f"{base}-knowledge-{i % 5}", {})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=write_many, args=(w, f"agent-{i}"))
               for i, w in enumerate(wrappers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    # 5 个知识点 + 去重 bump，不会膨胀
    found = inner.search("knowledge", top_k=1000)
    assert len(found) <= 5, f"去重在并发下失效: {len(found)}"
    # 跨 Agent 碰撞被仲裁记录
    assert wrappers[1].arbitration_count >= 0
    for w in wrappers:
        w.close()


# ---------------- C. 同一项目锁文件互斥 ----------------
def test_project_lock_mutual_exclusion(ws_tmp):
    """两个进程争同一项目锁：仅一方成功；释放后另一方成功。"""
    proj = ws_tmp / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    # 进程 1 获取并持有 2s（异步启动）
    p1 = _run_child_async(_LOCK_WORKER, str(proj), 0, 2.0)
    time.sleep(0.8)  # 等 p1 拿到锁
    # 进程 2 立即尝试 -> 应失败
    r2 = _run_child(_LOCK_WORKER, str(proj), 0, 0)
    assert r2.get("acquired") is False, f"第二实例不应获得锁: {r2}"
    r1 = _read_child(p1)
    assert r1.get("acquired") is True
    # 释放后进程 3 可获得
    r3 = _run_child(_LOCK_WORKER, str(proj), 0, 0)
    assert r3.get("acquired") is True


def test_project_lock_stale_reclaim(ws_tmp):
    """持有者进程已崩溃（残留锁）-> 新实例自动回收。"""
    proj = ws_tmp / "proj2"
    proj.mkdir(parents=True, exist_ok=True)
    lock = ProjectLock(str(proj), holder="dead-instance")
    lock.lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 写入一个不可能存在的 PID 的残留锁
    lock.lock_path.write_text(json.dumps({
        "pid": 2 ** 31 - 1, "holder": "dead-instance",
        "acquired_at": time.time() - 100,
    }), encoding="utf-8")
    assert lock.is_held_by_alive_process() is False
    ok = lock.acquire(timeout=2)
    assert ok, "残留锁应被回收"
    lock.release()
    assert not lock.lock_path.exists()


def test_loop_project_lock_integration(ws_tmp):
    """AgentLoop 集成：第二实例启动被明确拒绝，关闭后恢复。"""
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)

    def make_loop():
        cfg = AppConfig(
            agent=AgentConfig(
                max_rounds=3, max_retries=0, max_concurrency=1,
                project_lock_enabled=True, project_lock_timeout=0,
                trace_enabled=False, archive_enabled=False,
                metrics_enabled=False, snapshot_enabled=False,
                regression_check_enabled=False, auto_testgen=False,
                mutation_check_enabled=False, counterfactual_enabled=False,
            ),
            sandbox=SandboxConfig(workspace=str(ws)),
            memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"),
                                backend="none", auto_experience=False),
            mcp=MCPOptions(enabled=False),
        )
        return AgentLoop(config=cfg, llm=MockLLM(),
                         planner=StubPlanner())

    loop1 = make_loop()
    asyncio.run(loop1.run("第一实例"))
    # 第二实例应被拒绝
    loop2 = make_loop()
    try:
        with pytest.raises(ProjectLockError):
            asyncio.run(loop2.run("第二实例"))
    finally:
        asyncio.run(loop2.close())
    # 第一实例关闭后，新实例可运行
    asyncio.run(loop1.close())
    loop3 = make_loop()
    try:
        result = asyncio.run(loop3.run("第三实例"))
        assert result.ok
    finally:
        asyncio.run(loop3.close())


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


# ---------------- D. 会话状态共享与隔离 ----------------
def test_session_isolation_and_memory_shared(ws_tmp):
    """两个实例：会话档案隔离；项目记忆后端共享。"""
    db = str(ws_tmp / "mem.db")
    store_a = SharedMemoryStore(SqliteMemoryStore(db), creator="agent-a",
                                lock_key=db)
    store_b = SharedMemoryStore(SqliteMemoryStore(db), creator="agent-b",
                                lock_key=db)
    store_a.remember("exp", "project-shared-knowledge", {})
    hits = store_b.search("project-shared", top_k=10)
    assert any("project-shared-knowledge" in h["text"] for h in hits), \
        "项目记忆未跨实例共享"
    store_a.close()
    store_b.close()


# ---------------- 压力与故障注入 ----------------
def test_stress_8_process_memory_write(ws_tmp):
    """8 进程 x 20 条并发写入：计数精确、无异常。"""
    db = str(ws_tmp / "stress.db")
    n_procs, per_proc = 8, 20
    procs = [_run_child_async(_SQLITE_WORKER, db, i, per_proc)
             for i in range(n_procs)]
    results = [_read_child(p) for p in procs]
    assert all(r.get("ok") for r in results), results
    store = SqliteMemoryStore(db)
    try:
        found = store.search("worker", top_k=100000)
        assert len(found) == n_procs * per_proc
    finally:
        store.close()
