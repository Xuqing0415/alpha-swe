"""断点续跑测试（方案 1.3）：任务快照落盘 / 恢复 / 无快照回退重新规划。"""
import json
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task, TaskDAG, TaskStatus
from agent.llm import MockLLM


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class BoomPlanner:
    """resume 成功路径不应触发重新规划。"""

    async def plan(self, prompt, context=""):
        raise AssertionError("断点续跑不应重新规划任务")


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=0)]


def make_config(ws_tmp: Path):
    return AppConfig(
        agent=AgentConfig(
            max_rounds=10, max_retries=2, max_concurrency=1,
            snapshot_enabled=True,
            snapshot_dir=str(ws_tmp / "snapshots"),
            snapshot_keep=2,
        ),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


def test_task_snapshot_roundtrip():
    """快照序列化/反序列化保留状态、结果与重试/降级字段。"""
    dag = TaskDAG()
    a = Task(id="a", instruction="第一步", status=TaskStatus.COMPLETED,
             result="a-done", retry_count=1, max_retries=3,
             retry_strategy="retry_with_context", criticality="normal")
    b = Task(id="b", instruction="第二步", dependencies=["a"],
             status=TaskStatus.RUNNING, round_count=2)
    dag.add(a)
    dag.add(b)
    restored = TaskDAG.from_snapshot(dag.to_snapshot())
    ra = restored.get("a")
    rb = restored.get("b")
    assert ra.status == TaskStatus.COMPLETED
    assert ra.result == "a-done"
    assert ra.retry_count == 1
    assert ra.retry_strategy == "retry_with_context"
    assert ra.criticality == "normal"
    assert rb.dependencies == ["a"]
    assert rb.status == TaskStatus.RUNNING
    assert rb.round_count == 2


def test_save_snapshot_writes_and_prunes(ws_tmp, monkeypatch):
    """每步落盘快照并只保留最近 snapshot_keep 个。"""
    cfg = make_config(ws_tmp)
    loop = AgentLoop(config=cfg, llm=ScriptedLLM(), planner=StubPlanner())
    loop._current_prompt = "快照保存测试"
    dag = TaskDAG()
    dag.add(Task(id="a", instruction="a", status=TaskStatus.COMPLETED,
                 result="ok"))
    loop.scheduler.dag = dag

    counter = {"n": 0}

    def fake_strftime(fmt):
        counter["n"] += 1
        return f"20260811-{counter['n']:06d}"

    monkeypatch.setattr("agent.core.loop.time.strftime", fake_strftime)
    for _ in range(3):
        loop._save_snapshot()

    files = sorted((ws_tmp / "snapshots").glob("task_*.json"))
    assert len(files) == 2  # snapshot_keep=2
    assert files[0].name == "task_20260811-000002_step1.json"
    assert files[1].name == "task_20260811-000003_step1.json"
    latest = json.loads(files[1].read_text(encoding="utf-8"))
    assert latest["prompt"] == "快照保存测试"
    assert any(t["id"] == "a" for t in latest["tasks"])


@pytest.mark.asyncio
async def test_run_resume_restores_dag_without_replanning(ws_tmp):
    """resume=True 从快照恢复 DAG：已完成步骤保留结果，未完成步骤继续执行。"""
    cfg = make_config(ws_tmp)
    snap_dir = Path(cfg.agent.snapshot_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    dag = TaskDAG()
    dag.add(Task(id="a", instruction="已完成的步骤",
                 status=TaskStatus.COMPLETED, result="a-done"))
    dag.add(Task(id="b", instruction="待续步骤", dependencies=["a"],
                 status=TaskStatus.READY))
    data = dag.to_snapshot()
    data["prompt"] = "断点续跑测试"
    (snap_dir / "task_20260811-000001_step1.json").write_text(
        json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")

    loop = AgentLoop(config=cfg, llm=ScriptedLLM('{"final_answer": "b 完成"}'),
                     planner=BoomPlanner())
    result = await loop.run("断点续跑测试", resume=True)
    assert result.ok
    assert loop.scheduler.dag.get("a").status == TaskStatus.COMPLETED
    assert loop.scheduler.dag.get("a").result == "a-done"
    assert loop.scheduler.dag.get("b").status == TaskStatus.COMPLETED
    assert "b 完成" in result.final_answer
    names = [r.get("name") for r in loop._decision.records()]
    assert "resume.restored" in names
    assert "resume.no_snapshot" not in names


@pytest.mark.asyncio
async def test_run_resume_falls_back_to_replanning_without_snapshot(ws_tmp):
    """resume=True 但没有快照时回退重新规划，并记录 resume.no_snapshot。"""
    cfg = make_config(ws_tmp)
    loop = AgentLoop(config=cfg, llm=ScriptedLLM('{"final_answer": "从头规划"}'),
                     planner=StubPlanner())
    result = await loop.run("无快照恢复测试", resume=True)
    assert result.ok
    assert loop.scheduler.dag.get("t0").status == TaskStatus.COMPLETED
    names = [r.get("name") for r in loop._decision.records()]
    assert "resume.no_snapshot" in names


@pytest.mark.asyncio
async def test_run_snapshot_enabled_writes_file_on_step_done(ws_tmp):
    """正常执行（非 resume）每完成一个子步骤也自动落盘快照。"""
    cfg = make_config(ws_tmp)
    loop = AgentLoop(config=cfg, llm=ScriptedLLM('{"final_answer": "完成"}'),
                     planner=StubPlanner())
    result = await loop.run("自动快照测试")
    assert result.ok
    files = list((ws_tmp / "snapshots").glob("task_*.json"))
    assert files, "任务完成后应产生快照文件"
    data = json.loads(sorted(files, key=lambda p: p.stat().st_mtime)[-1]
                      .read_text(encoding="utf-8"))
    assert data["prompt"] == "自动快照测试"
    assert any(t["status"] == "completed" for t in data["tasks"])