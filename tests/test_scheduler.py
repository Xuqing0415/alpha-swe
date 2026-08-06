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