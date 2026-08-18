"""调度器测试：依赖拓扑、优先级、WAITING 唤醒。"""
import asyncio

import pytest

from agent.core.scheduler import Scheduler
from agent.core.task import TaskDAG, TaskStatus


@pytest.mark.asyncio
async def test_dependency_order():
    dag = TaskDAG()
    a = dag.create_task("a", task_id="a")
    b = dag.create_task("b", dependencies=["a"], task_id="b")
    c = dag.create_task("c", priority=1, task_id="c")
    sched = Scheduler(dag, max_concurrency=1)
    order = []

    async def worker(task):
        order.append(task.id)
        task.mark(TaskStatus.COMPLETED, result=f"{task.id}-done")

    sched.set_worker(worker)
    sched.submit_plan([a, b, c])
    await sched.run_to_completion()
    # c 优先级最高，其次 a（无依赖），b 必须等 a
    assert order == ["c", "a", "b"]
    assert b.result == "b-done"


@pytest.mark.asyncio
async def test_waiting_resume_via_wake():
    dag = TaskDAG()
    t = dag.create_task("wait", task_id="w")
    sched = Scheduler(dag)
    runs = {"n": 0}

    async def worker(task):
        runs["n"] += 1
        if runs["n"] == 1:
            task.mark(TaskStatus.WAITING)
            return
        task.mark(TaskStatus.COMPLETED, result="done")

    sched.set_worker(worker)
    sched.submit_plan([t])

    async def waker():
        await asyncio.sleep(0.05)
        sched.wake()

    w = asyncio.create_task(waker())
    await sched.run_to_completion()
    await w
    assert runs["n"] == 2
    assert t.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_failed_dependency_does_not_hang():
    """前置任务失败时，依赖它的 WAITING 任务必须被级联终止（真实 LLM 场景回归）。"""
    dag = TaskDAG()
    t0 = dag.create_task("t0", task_id="t0")
    t1 = dag.create_task("t1", dependencies=["t0"], task_id="t1")
    t2 = dag.create_task("t2", dependencies=["t1"], task_id="t2")
    sched = Scheduler(dag, max_concurrency=1)

    async def worker(task):
        if task.id == "t0":
            task.mark(TaskStatus.FAILED, error="budget exhausted")
            return
        task.mark(TaskStatus.COMPLETED, result="ok")

    sched.set_worker(worker)
    sched.submit_plan([t0, t1, t2])
    await asyncio.wait_for(sched.run_to_completion(), timeout=5)
    assert t0.status == TaskStatus.FAILED
    assert t1.status == TaskStatus.FAILED
    assert t2.status == TaskStatus.FAILED
    assert "t0" in (t1.error or "")


@pytest.mark.asyncio
async def test_noncritical_dependent_skipped_after_failure():
    """normal/optional 依赖者在关键任务失败后被标记 SKIPPED，不阻塞其它分支。"""
    dag = TaskDAG()
    root = dag.create_task("root", task_id="root")
    dep_crit = dag.create_task("dep-crit", dependencies=["root"], task_id="dc")
    dep_crit.criticality = "critical"
    dep_norm = dag.create_task("dep-norm", dependencies=["root"], task_id="dn")
    dep_norm.criticality = "normal"
    other = dag.create_task("other", task_id="other")
    sched = Scheduler(dag, max_concurrency=1)

    async def worker(task):
        if task.id == "root":
            task.mark(TaskStatus.FAILED, error="boom")
            return
        task.mark(TaskStatus.COMPLETED, result="ok")

    sched.set_worker(worker)
    sched.submit_plan([root, dep_crit, dep_norm, other])
    await asyncio.wait_for(sched.run_to_completion(), timeout=5)
    assert root.status == TaskStatus.FAILED
    assert dep_crit.status == TaskStatus.FAILED
    assert dep_norm.status == TaskStatus.SKIPPED
    assert other.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_spawn_adds_task_during_run():
    dag = TaskDAG()
    t0 = dag.create_task("t0", task_id="t0")
    sched = Scheduler(dag, max_concurrency=1)
    spawned = []

    async def worker(task):
        if task.id == "t0" and not spawned:
            spawned.append(sched.spawn("子任务", parent_id="t0"))
        task.mark(TaskStatus.COMPLETED, result="ok")

    sched.set_worker(worker)
    sched.submit_plan([t0])
    await sched.run_to_completion()
    assert len(spawned) == 1
    assert dag.get(spawned[0].id).status == TaskStatus.COMPLETED