"""Task/TaskDAG 测试。"""
from agent.core.task import Task, TaskDAG, TaskStatus


def test_create_and_query():
    dag = TaskDAG()
    t = dag.create_task("task-1", task_id="t1")
    assert dag.get("t1") is t
    assert t.status == TaskStatus.IDLE
    assert dag.summary()["total"] == 1


def test_ready_ordering_by_priority():
    dag = TaskDAG()
    low = dag.create_task("low", priority=0, task_id="low")
    high = dag.create_task("high", priority=10, task_id="high")
    low.mark(TaskStatus.READY)
    high.mark(TaskStatus.READY)
    assert dag.ready_tasks() == [high, low]


def test_dependencies_gate_ready():
    dag = TaskDAG()
    a = dag.create_task("a", task_id="a")
    b = dag.create_task("b", dependencies=["a"], task_id="b")
    a.mark(TaskStatus.READY)
    b.mark(TaskStatus.READY)
    # b 依赖 a，a 未完成时 b 不可就绪
    assert dag.ready_tasks() == [a]
    dag.mark("a", TaskStatus.COMPLETED, result="ok")
    assert dag.ready_tasks() == [b]


def test_promote_dependents():
    dag = TaskDAG()
    a = dag.create_task("a", task_id="a")
    b = dag.create_task("b", dependencies=["a"], task_id="b")
    a.mark(TaskStatus.READY)
    dag.mark("a", TaskStatus.COMPLETED)
    promoted = dag.promote_dependents("a")
    assert [t.id for t in promoted] == ["b"]
    assert b.status == TaskStatus.READY


def test_children_of_parent():
    dag = TaskDAG()
    dag.create_task("sub-1", parent_id="root", task_id="s1")
    dag.create_task("sub-2", parent_id="root", task_id="s2")
    dag.create_task("other", task_id="o1")
    assert len(dag.children("root")) == 2


def test_pending_and_summary():
    dag = TaskDAG()
    dag.create_task("a", task_id="a")
    dag.create_task("b", task_id="b")
    dag.mark("a", TaskStatus.COMPLETED)
    assert dag.pending() is True  # b 仍 idle
    dag.mark("b", TaskStatus.FAILED, error="boom")
    assert dag.pending() is False
    assert dag.summary()["by_status"]["completed"] == 1
    assert dag.summary()["by_status"]["failed"] == 1