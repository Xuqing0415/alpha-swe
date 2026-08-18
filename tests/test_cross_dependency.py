# -*- coding: utf-8 -*-
"""进阶 2.2 跨任务依赖感知测试：依赖等待、promote 恢复、调度顺序与 TUI 展示。

覆盖：
- submit/spawn 依赖未满足时进入 WAITING（_waiting_reason=dependency）；
- promote_dependents 只提升依赖等待，不提升外部等待；
- 调度循环保证被依赖任务先完成；
- Planner 按复杂度为子任务估算预算；
- TUI 任务行对依赖等待任务显示「依赖」标记。
"""

import pytest

from agent.config import PlannerConfig
from agent.core.scheduler import Scheduler
from agent.core.task import Task, TaskDAG, TaskStatus
from agent.planner.planner import Planner


def test_submit_dependency_waiting():
    dag = TaskDAG()
    a = dag.create_task("a", task_id="a")
    b = dag.create_task("b", task_id="b", dependencies=["a"])
    sched = Scheduler(dag)
    sched.submit(b)
    assert b.status == TaskStatus.WAITING
    assert b.metadata["_waiting_reason"] == "dependency"
    sched.submit(a)
    assert a.status == TaskStatus.READY


def test_spawn_dependency_waiting():
    dag = TaskDAG()
    a = dag.create_task("a", task_id="a")
    sched = Scheduler(dag)
    sched.submit(a)
    b = sched.spawn("B1", dependencies=["a"])
    assert b.status == TaskStatus.WAITING
    assert b.metadata["_waiting_reason"] == "dependency"


def test_promote_dependents_promotes_dependency_waiting():
    dag = TaskDAG()
    a = dag.create_task("a", task_id="a")
    b = dag.create_task("b", task_id="b", dependencies=["a"])
    sched = Scheduler(dag)
    sched.submit(b)
    assert b.status == TaskStatus.WAITING
    a.mark(TaskStatus.COMPLETED, result="ok")
    sched.dag.promote_dependents("a")
    assert b.status == TaskStatus.READY
    assert "_waiting_reason" not in b.metadata


def test_external_waiting_not_promoted_by_dependency():
    dag = TaskDAG()
    a = dag.create_task("a", task_id="a")
    b = dag.create_task("b", task_id="b", dependencies=["a"])
    b.mark(TaskStatus.WAITING)  # 外部等待（如后台任务），无 _waiting_reason
    a.mark(TaskStatus.COMPLETED, result="ok")
    assert dag.promote_dependents("a") == []
    assert b.status == TaskStatus.WAITING


@pytest.mark.asyncio
async def test_cross_task_dependency_scheduling():
    dag = TaskDAG()
    a = dag.create_task("a", task_id="a")
    b = dag.create_task("b", task_id="b", dependencies=["a"])
    sched = Scheduler(dag, max_concurrency=1)
    order = []

    async def worker(task):
        order.append(task.id)
        task.mark(TaskStatus.COMPLETED, result="ok")

    sched.set_worker(worker)
    sched.submit(a)
    sched.submit(b)  # 依赖 a 未完成 -> WAITING
    await sched.run_to_completion()
    assert order == ["a", "b"]
    assert a.status == TaskStatus.COMPLETED
    assert b.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_planner_fills_budgets():
    class P:
        def __init__(self, raw):
            self.raw = raw

        async def complete(self, messages):
            return self.raw

    prompt = "修复登录空指针并同时补充单元测试、重构鉴权模块、更新文档"
    planner = Planner(
        llm=P('[{"instruction": "定位空指针位置", "dependencies": []}]'),
        config=PlannerConfig(split_threshold_complexity=0.0),
    )
    tasks = await planner.plan(prompt)
    assert len(tasks) == 1
    assert tasks[0].token_budget and tasks[0].time_budget


def test_estimate_budget_scales_with_complexity():
    simple = Planner._estimate_budget("改个变量名", PlannerConfig())
    complex_ = Planner._estimate_budget(
        "重构多个模块并同时处理依赖、添加测试、更新文档", PlannerConfig())
    assert simple[0] < complex_[0]
    assert simple[1] < complex_[1]
    assert simple[0] >= 2000
    assert simple[1] >= 60.0


def test_task_row_dependency_mark():
    from tui.app import _task_row
    t = Task(id="t", instruction="B1 修改登录", dependencies=["a"])
    t.mark(TaskStatus.WAITING)
    t.metadata["_waiting_reason"] = "dependency"
    assert "依赖 " in _task_row(t)
    t2 = Task(id="t2", instruction="外部等待")
    t2.mark(TaskStatus.WAITING)
    assert "等待 " in _task_row(t2)

def test_render_dependency_tree_ascii_lines():
    """依赖图以 ASCII 连线树渲染：|-- 兄弟 / +-- 末位 / 等依赖标记。"""
    from tui.app import _render_dependency_tree
    dag = TaskDAG()
    dag.create_task("根任务", task_id="a")
    b = dag.create_task("依赖A", task_id="b",
                        dependencies=["a"])
    dag.create_task("依赖B", task_id="c",
                    dependencies=["b"])
    b.metadata["_waiting_reason"] = "dependency"
    b.mark(TaskStatus.WAITING)
    text = _render_dependency_tree(list(dag.all()))
    plain = text.plain
    assert "a 根任务" in plain
    assert "+-- b 依赖A [等依赖]" in plain
    assert "    +-- c 依赖B" in plain
    # 连线只用 ASCII 的 | - + 与空格，不引入特殊 Unicode 框线字符
    box = set("─│┌┐└┘├┤┬┴┼")
    assert not (box & set(plain))
    assert "+--" in plain


def test_render_dependency_tree_cycle_marked():
    """成环依赖渲染为 (环，见上)，不无限递归。"""
    from tui.app import _render_dependency_tree
    dag = TaskDAG()
    a = dag.create_task("A", task_id="a")
    dag.create_task("B", task_id="b",
                    dependencies=["a"])
    a.dependencies = ["b"]
    text = _render_dependency_tree(list(dag.all()))
    assert "环，见上" in text.plain

