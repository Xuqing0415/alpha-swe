# -*- coding: utf-8 -*-
"""进阶 2.1 任务队列与动态抢占测试：PAUSED 状态、抢占调度、恢复与 TUI 命令。

覆盖：
- TaskStatus.PAUSED 序列化；
- Scheduler.pending_higher / promote_paused / set_priority；
- 低优先级任务被高优先级任务抢占 -> 高优先级先完成 -> 低优先级恢复；
- AgentLoop 安全点抢占 + 抢占点快照 + task_resumed 事件；
- set_task_priority 命令入口。
"""
import asyncio
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.scheduler import Scheduler
from agent.core.task import Task, TaskDAG, TaskStatus
from agent.llm import MockLLM


class StubPlanner:
    def __init__(self, task_id: str = "t0"):
        self.task_id = task_id

    async def plan(self, prompt, context=""):
        return [Task(id=self.task_id, instruction=prompt, priority=0,
                     criticality="critical")]


class SlowScriptedLLM(MockLLM):
    """带延迟的脚本化 LLM：让抢占协程有窗口插入高优先级任务。"""

    def __init__(self, *responses, delay: float = 0.05):
        self._responses = list(responses)
        self._delay = delay

    async def complete(self, messages):
        await asyncio.sleep(self._delay)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp: Path, *, snapshot: bool = False):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1,
                          snapshot_enabled=snapshot,
                          snapshot_dir=str(ws_tmp / "snap")),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"),
                            auto_experience=False),
        mcp=MCPOptions(enabled=False),
    )


def test_format_preemption_events():
    """新事件（抢占/恢复/优先级调整）在 TUI 日志中的渲染。"""
    from tui.formatting import format_event

    t1 = format_event({"type": "task_preempted",
                       "data": {"task_id": "a", "priority": 0}})
    assert "任务被抢占暂停" in t1.plain and "PRQ" in t1.plain
    t2 = format_event({"type": "task_resumed",
                       "data": {"task_id": "a", "priority": 0}})
    assert "任务恢复执行" in t2.plain
    t3 = format_event({"type": "priority_changed",
                       "data": {"task_id": "a", "priority": 5}})
    assert "优先级调整" in t3.plain


def test_paused_status_serialization():
    dag = TaskDAG()
    t = dag.create_task("t", task_id="t", priority=3)
    t.mark(TaskStatus.PAUSED)
    restored = TaskDAG.from_snapshot(dag.to_snapshot())
    rt = restored.get("t")
    assert rt.status == TaskStatus.PAUSED
    assert rt.priority == 3
    assert TaskStatus.PAUSED.value == "paused"


def test_pending_higher_priority_detection():
    dag = TaskDAG()
    low = dag.create_task("low", task_id="low", priority=0)
    high = dag.create_task("high", task_id="high", priority=10)
    sched = Scheduler(dag)
    low.mark(TaskStatus.READY)
    high.mark(TaskStatus.READY)
    assert sched.pending_higher(low)
    assert not sched.pending_higher(high)
    sched.set_priority("high", 0)
    assert not sched.pending_higher(low)
    # 依赖未满足的 READY 任务不参与抢占判定
    c = dag.create_task("c", task_id="c", dependencies=["high"], priority=20)
    c.mark(TaskStatus.READY)
    assert not sched.pending_higher(low)


def test_promote_paused_waits_for_higher():
    dag = TaskDAG()
    low = dag.create_task("low", task_id="low", priority=0)
    high = dag.create_task("high", task_id="high", priority=10)
    sched = Scheduler(dag)
    low.mark(TaskStatus.PAUSED)
    high.mark(TaskStatus.READY)
    assert sched.promote_paused() == []
    assert low.status == TaskStatus.PAUSED
    high.mark(TaskStatus.COMPLETED, result="done")
    promoted = sched.promote_paused()
    assert [t.id for t in promoted] == ["low"]
    assert low.status == TaskStatus.READY


@pytest.mark.asyncio
async def test_set_priority_reorders_ready():
    dag = TaskDAG()
    a = dag.create_task("a", task_id="a", priority=0)
    b = dag.create_task("b", task_id="b", priority=0)
    sched = Scheduler(dag, max_concurrency=1)
    order = []

    async def worker(task):
        order.append(task.id)
        task.mark(TaskStatus.COMPLETED, result="ok")

    sched.set_worker(worker)
    sched.submit_plan([a, b])
    sched.set_priority("a", 10)
    await sched.run_to_completion()
    assert order == ["a", "b"]


@pytest.mark.asyncio
async def test_preemption_pauses_running_task():
    dag = TaskDAG()
    low = dag.create_task("low", task_id="low", priority=0)
    sched = Scheduler(dag, max_concurrency=1)
    order = []
    runs = {"low": 0}
    high_id = []

    async def worker(task):
        if task.id == "low":
            runs["low"] += 1
            if runs["low"] == 1:
                # 低优先级执行中，出现更高优先级任务
                t = sched.spawn("高优先级任务", priority=10)
                high_id.append(t.id)
                order.append("low-paused")
                task.mark(TaskStatus.PAUSED)
                return
            order.append("low")
            task.mark(TaskStatus.COMPLETED, result="low-done")
        else:
            order.append("high")
            task.mark(TaskStatus.COMPLETED, result="high-done")

    sched.set_worker(worker)
    sched.submit(low)
    await sched.run_to_completion()
    assert order == ["low-paused", "high", "low"]
    assert dag.get("low").status == TaskStatus.COMPLETED
    assert dag.get(high_id[0]).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_loop_preemption_pauses_and_resumes(ws_tmp):
    """低优先级任务执行中被高优先级任务抢占：PAUSED -> 高优先级先完成 -> 恢复。"""
    cfg = make_config(ws_tmp, snapshot=True)
    llm = SlowScriptedLLM(
        '{"think": "低优先级工作中"}',
        '{"final_answer": "高优先级完成"}',
        '{"final_answer": "低优先级完成"}',
        delay=0.05,
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(task_id="t0"))
    captured = {}

    async def _preempt_soon():
        while True:
            low = loop.scheduler.dag.get("t0")
            # 等低优先级任务完成首轮思考（round_count>=1）再注入抢占，
            # 保证抢占发生在任务执行中途而非首轮 checkpoint 之前
            if (low is not None and low.status == TaskStatus.RUNNING
                    and low.round_count >= 1):
                captured["high"] = loop.scheduler.spawn("高优先级任务",
                                                        priority=10)
                return
            await asyncio.sleep(0.005)

    preemptor = asyncio.create_task(_preempt_soon())
    result = await loop.run("抢占测试")
    await preemptor

    assert result.ok
    types = [e["type"] for e in loop.events]
    assert "task_preempted" in types, types
    assert "task_resumed" in types, types
    high = captured["high"]
    low = loop.scheduler.dag.get("t0")
    assert high.status == TaskStatus.COMPLETED
    assert low.status == TaskStatus.COMPLETED
    preempt_i = types.index("task_preempted")
    done_i = next(
        i for i, e in enumerate(loop.events)
        if e["type"] == "task_done" and e["data"].get("task_id") == high.id
    )
    resume_i = next(
        i for i, e in enumerate(loop.events)
        if e["type"] == "task_resumed" and e["data"].get("task_id") == "t0"
    )
    assert preempt_i < done_i < resume_i
    # 抢占点快照落盘（含 PAUSED 状态）
    snaps = list((ws_tmp / "snap").glob("task_*.json"))
    assert snaps
    # 抢占前上下文保留：低优先级任务的思考发生在抢占之前
    think_i = next(
        i for i, e in enumerate(loop.events)
        if e["type"] == "think"
        and "低优先级工作中" in str(e["data"].get("content", ""))
    )
    assert think_i < preempt_i


@pytest.mark.asyncio
async def test_loop_set_task_priority(ws_tmp):
    cfg = make_config(ws_tmp)
    loop = AgentLoop(config=cfg, llm=MockLLM(), planner=StubPlanner())
    loop.scheduler.submit(Task(id="t0", instruction="x", priority=0))
    task = loop.set_task_priority("t0", 5)
    assert task is not None
    assert task.priority == 5
    assert any(e["type"] == "priority_changed" for e in loop.events)
    assert loop.set_task_priority("nope", 1) is None