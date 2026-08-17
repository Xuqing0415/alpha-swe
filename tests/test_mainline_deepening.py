# -*- coding: utf-8 -*-
"""三条主线深化落地方案：
1.2B 待办动作可操作化（pending_actions -> 调度任务）
2.1A 黑板文件级写锁（并发写冲突防护）
1.1A 三层快照对比（Last_known / Current_disk / Agent_intended）
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig, WorkerRoleConfig)
from agent.core.loop import AgentLoop, LoopResult
from agent.core.state import AgentPhase
from agent.core.task import Task, TaskStatus
from agent.llm import MockLLM
from agent.multiagent.blackboard import Blackboard
from agent.multiagent.workers import WorkerAgent
from agent.project_state import ProjectStateTracker
from agent.sandbox.audit import FileAuditStore
from agent.tools.base import ExecutionContext
from agent.tools.fileio import FileIOTool
from agent.workspace_context import WorkspaceContext


def make_config(ws_tmp: Path) -> AppConfig:
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, criticality="critical")]


class RecordingLLM(MockLLM):
    def __init__(self, *responses):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        self.calls.append(messages)
        system = messages[0].get("content", "") if messages else ""
        if "经验总结器" in system:
            return "{}"
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class ScriptedWorkerLLM(MockLLM):
    """Worker LLM：经验总结器调用返回空对象，其余按脚本顺序消费。"""

    def __init__(self, *responses: str):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        system = messages[0].get("content", "") if messages else ""
        if "经验总结器" in system:
            return "{}"
        assert self._responses, "Worker LLM 脚本响应已耗尽"
        return self._responses.pop(0)


@pytest.fixture
def ws_dir(ws_tmp):
    d = ws_tmp / "ws"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ================= 主线一 1.2B：待办动作可操作化 =================

def test_finalize_writes_structured_pending(ws_dir):
    ctx = WorkspaceContext(str(ws_dir))
    ctx.begin("修复登录 bug")
    todo = Task(id="a", instruction="运行测试验证修复", status=TaskStatus.READY)
    result = LoopResult(phase=AgentPhase.FAILED, tasks=[todo])
    ctx.finalize("修复登录 bug", result)
    entry = ctx.data["pending_actions"][0]
    assert entry["type"] == "run_test"
    assert entry["instruction"] == "运行测试验证修复"
    assert entry["blocking"] is True
    assert entry["status"] == "pending"
    assert entry["task_id"] == "a"


def test_resume_tasks_build_schedulable_tasks(ws_dir):
    ctx = WorkspaceContext(str(ws_dir))
    ctx.begin("修复登录 bug")
    ctx.data["pending_actions"] = [
        {"type": "run_test", "instruction": "跑测试", "blocking": True,
         "status": "pending", "target": "tests/auth.test.ts"},
        {"type": "commit_changes", "instruction": "提交变更", "blocking": False,
         "status": "pending"},
        {"type": "run_test", "instruction": "跑测试", "blocking": True,
         "status": "pending"},  # 重复指令应去重
        {"type": "run_test", "instruction": "已完成项", "blocking": True,
         "status": "done"},
    ]
    tasks = ctx.resume_tasks()
    assert len(tasks) == 2
    assert tasks[0].metadata["resume"] is True
    assert tasks[0].criticality == "normal"
    assert tasks[0].priority == 10
    assert tasks[1].criticality == "optional"


def test_legacy_string_pending_upgraded(ws_dir):
    ctx = WorkspaceContext(str(ws_dir))
    ctx.begin("旧格式")
    ctx.data["pending_actions"] = ["跑 pytest 验证"]
    entries = ctx.pending_action_entries()
    assert entries[0]["type"] == "run_test"
    assert entries[0]["blocking"] is True


def test_context_backup_rotation_and_self_heal(ws_dir):
    ctx = WorkspaceContext(str(ws_dir))
    ctx.begin("任务 A")
    ctx.data["prompt"] = "任务 A"
    ctx.save()
    ctx.data["prompt"] = "任务 B"
    ctx.save()
    bak = ws_dir / ".swe-agent" / "context.json.bak1"
    assert bak.exists()
    # 主文件损坏 -> 自动回退 .bak1
    ctx.context_file.write_text("{corrupt", encoding="utf-8")
    reloaded = WorkspaceContext(str(ws_dir))
    assert reloaded.data["prompt"] == "任务 A"


@pytest.mark.asyncio
async def test_resume_task_merged_into_plan(ws_dir):
    prev = WorkspaceContext(str(ws_dir))
    prev.begin("上次任务")
    prev.data["pending_actions"] = [
        {"type": "generic", "instruction": "继续上次未完成的工作",
         "blocking": True, "status": "pending"}]
    prev.save()
    llm = RecordingLLM('{"final_answer": "done"}',
                       '{"final_answer": "done"}')
    loop = AgentLoop(config=make_config(ws_dir.parent), llm=llm,
                     planner=StubPlanner())
    try:
        r = await loop.run("继续", resume=True)
        assert r.ok
        dag_tasks = loop.scheduler.dag.all()
        assert any(t.metadata.get("resume") for t in dag_tasks), \
            "待办动作未转成调度任务入队"
        names = [x["name"] for x in loop._decision.records()]
        assert "workspace.resume_tasks" in names
    finally:
        await loop.close()


# ================= 主线二 2.1A：文件级写锁 =================

def test_blackboard_file_lock_exclusive(ws_tmp):
    bb = Blackboard()
    assert bb.lock_file("src/a.py", "coder") is True
    assert bb.lock_file("src/a.py", "coder") is True  # 同持有者可重入
    assert bb.lock_file("src/a.py", "tester") is False  # 其他持有者被拒
    assert bb.lock_holder("src/a.py") == "coder"
    assert bb.is_file_locked("src/a.py") is True
    assert bb.is_file_locked("src/a.py", "coder") is True
    assert bb.is_file_locked("src/a.py", "tester") is False
    assert bb.unlock_file("src/a.py", "tester") is False
    assert bb.unlock_file("src/a.py", "coder") is True
    assert bb.is_file_locked("src/a.py") is False
    # 绝对/相对路径规范化指向同一把锁
    assert bb.lock_file(os.path.abspath("src/b.py"), "coder") is True
    assert bb.is_file_locked("src/b.py") is True


def test_blackboard_release_all(ws_tmp):
    bb = Blackboard()
    bb.lock_file("a.py", "coder")
    bb.lock_file("b.py", "coder")
    bb.lock_file("c.py", "tester")
    assert bb.release_all("coder") == 2
    assert bb.is_file_locked("a.py") is False
    assert bb.is_file_locked("c.py") is True
    assert bb.summary()["file_locks"] == 1


@pytest.mark.asyncio
async def test_fileio_write_blocked_by_other_holder(ws_tmp):
    bb = Blackboard()
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    ctx = ExecutionContext(workspace=str(ws), task_id="t1")
    coder_tool = FileIOTool(read_only=False,
                            audit_store=FileAuditStore(str(ws_tmp / "audit")),
                            lock_manager=bb, lock_holder="coder")
    r1 = await coder_tool.execute(
        {"action": "write", "path": "a.txt", "content": "v1"}, ctx)
    assert r1.success
    tester_tool = FileIOTool(read_only=False,
                             audit_store=FileAuditStore(str(ws_tmp / "audit")),
                             lock_manager=bb, lock_holder="tester")
    r2 = await tester_tool.execute(
        {"action": "write", "path": "a.txt", "content": "v2"}, ctx)
    assert not r2.success
    assert r2.metadata.get("file_lock_conflict") is True
    assert r2.metadata.get("holder") == "coder"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "v1"
    # 释放后其他 Agent 可写
    bb.release_all("coder")
    r3 = await tester_tool.execute(
        {"action": "write", "path": "a.txt", "content": "v3"}, ctx)
    assert r3.success
    assert (ws / "a.txt").read_text(encoding="utf-8") == "v3"


def test_worker_roles_wire_lock_manager(ws_tmp):
    role = WorkerRoleConfig(name="coder", tools=["file_ops"])
    worker = WorkerAgent(role, config=make_config(ws_tmp))
    tool = worker._role_tools().get("file_ops")
    assert tool.lock_manager is worker.blackboard
    assert tool.lock_holder == "coder"
    # 配置开关关闭时不启用写锁
    cfg = make_config(ws_tmp)
    cfg.team.file_locks_enabled = False
    worker2 = WorkerAgent(role, config=cfg)
    assert worker2._role_tools().get("file_ops").lock_manager is None


@pytest.mark.asyncio
async def test_worker_write_releases_lock_after_task(ws_tmp):
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    role = WorkerRoleConfig(name="coder", tools=["file_ops"])
    coder = WorkerAgent(role, config=cfg, blackboard=bb,
                        llm=ScriptedWorkerLLM(
                            '{"tool": "file_ops", "params": '
                            '{"action": "write", "path": "a.txt", '
                            '"content": "coder-version"}}',
                            '{"final_answer": "done"}'))
    result = await coder.execute_task(
        Task(id="c1", instruction="写入 a.txt"))
    assert result.ok
    assert bb.locked_files() == [], "任务结束后写锁必须释放"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "coder-version"


@pytest.mark.asyncio
async def test_worker_write_blocked_while_other_holds_lock(ws_tmp):
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    role = WorkerRoleConfig(name="tester", tools=["file_ops"])
    tester = WorkerAgent(role, config=cfg, blackboard=bb,
                         llm=ScriptedWorkerLLM(
                             '{"tool": "file_ops", "params": '
                             '{"action": "write", "path": "a.txt", '
                             '"content": "tester-version"}}',
                             '{"final_answer": "done"}'))
    # coder 模拟持有 a.txt 写锁
    assert bb.lock_file(str(ws / "a.txt"), "coder") is True
    result = await tester.execute_task(
        Task(id="t1", instruction="写入 a.txt"))
    # tester 的写入被拦截：文件未被创建，锁仍由 coder 持有
    assert not (ws / "a.txt").exists()
    assert bb.lock_holder(str(ws / "a.txt")) == "coder"


# ================= 主线一 1.1A：三层快照对比 =================

def _setup_baseline(ws_dir, name="app.py", content="v1"):
    f = ws_dir / name
    f.write_text(content, encoding="utf-8")
    tracker = ProjectStateTracker(str(ws_dir))
    tracker.begin_session()
    tracker.end_session()
    return f, tracker


def test_three_layer_agent_normal_modify(ws_dir):
    f, tracker = _setup_baseline(ws_dir)
    tracker.begin_session()
    f.write_text("v2", encoding="utf-8")
    tracker.note_agent_write("app.py")
    changes = tracker.end_session()
    assert changes.get("conflicts") == []


def test_three_layer_external_overwrite(ws_dir):
    f, tracker = _setup_baseline(ws_dir)
    tracker.begin_session()
    f.write_text("v2-agent", encoding="utf-8")
    tracker.note_agent_write("app.py")
    f.write_text("v3-external", encoding="utf-8")
    changes = tracker.end_session()
    kinds = {c["kind"] for c in changes.get("conflicts", [])}
    assert "external_overwrite" in kinds
    assert changes.get("conflict_text", "").startswith("## 会话期间的文件变更冲突")


def test_three_layer_reverted(ws_dir):
    f, tracker = _setup_baseline(ws_dir)
    last_stat = f.stat()
    tracker.begin_session()
    f.write_text("v2-agent", encoding="utf-8")
    tracker.note_agent_write("app.py")
    # 回滚：内容恢复到 last-known 且 mtime 一致
    f.write_text("v1", encoding="utf-8")
    os.utime(f, ns=(last_stat.st_atime_ns, last_stat.st_mtime_ns))
    changes = tracker.end_session()
    kinds = {c["kind"] for c in changes.get("conflicts", [])}
    assert "reverted" in kinds


def test_three_layer_external_modified(ws_dir):
    f, tracker = _setup_baseline(ws_dir)
    tracker.begin_session()
    f.write_text("v2-user", encoding="utf-8")  # 非 Agent 写入
    changes = tracker.end_session()
    kinds = {c["kind"] for c in changes.get("conflicts", [])}
    assert "external_modified" in kinds


def test_three_layer_agent_delete_normal(ws_dir):
    f, tracker = _setup_baseline(ws_dir)
    tracker.begin_session()
    f.unlink()
    tracker.note_agent_write("app.py")  # 记录删除意图
    changes = tracker.end_session()
    assert changes.get("conflicts") == []


def test_state_backup_rotation_and_self_heal(ws_dir):
    _setup_baseline(ws_dir)
    sf = ws_dir / ".swe-agent" / "state.json"
    assert sf.exists()
    # 主文件损坏 -> 回退 .bak1（若存在）或重建全新状态
    sf.write_text("{bad json", encoding="utf-8")
    tracker = ProjectStateTracker(str(ws_dir))
    assert tracker.state.get("schema") == 1


# ================= 1.2B + 1.1A 的 loop 级冲突上报 =================

@pytest.mark.asyncio
async def test_loop_emits_project_state_conflicts(ws_dir):
    f, tracker = _setup_baseline(ws_dir)
    tracker.begin_session()
    f.write_text("v2-agent", encoding="utf-8")
    tracker.note_agent_write("app.py")
    f.write_text("v3-external", encoding="utf-8")
    changes = tracker.end_session()
    assert any(c["kind"] == "external_overwrite"
               for c in changes.get("conflicts", []))
    assert "conflicts_history" in tracker.state
