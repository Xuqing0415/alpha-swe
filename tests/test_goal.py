# -*- coding: utf-8 -*-
"""Goal（目标驱动）基础模块测试。

覆盖：
- 状态机：合法迁移链与非法迁移保护；
- 进度推导：子目标均值、无子目标保值、已完成保 1.0；
- 序列化：to_dict / from_dict 递归往返与非法输入兜底；
- Prompt 注入：goal_prompt_block 结构与内容；
- 目标转任务：to_tasks 的子目标分支与单目标分支；
- 台账：GoalStore 原子落盘往返、损坏文件兜底、enabled=False 纯内存。
"""
import json

import pytest

from agent.core.goal import Goal, GoalStatus, GoalStore
from agent.core.task import TaskStatus


def _goal(goal_id, description, status=GoalStatus.PENDING, progress=0.0):
    """构造测试用目标。"""
    return Goal(id=goal_id, description=description, status=status, progress=progress)


def test_transition_chain_reaches_terminal():
    """pending -> approved -> in_progress -> completed 合法链。"""
    goal = _goal("g1", "接入目标驱动")
    assert goal.status is GoalStatus.PENDING
    assert goal.can_transition_to(GoalStatus.APPROVED) is True
    assert goal.transition(GoalStatus.APPROVED) is True
    assert goal.transition(GoalStatus.IN_PROGRESS) is True
    assert goal.is_terminal is False
    assert goal.transition(GoalStatus.COMPLETED) is True
    assert goal.status is GoalStatus.COMPLETED
    assert goal.progress == 1.0
    assert goal.is_terminal is True


def test_failed_goal_can_resume():
    """failed -> in_progress 允许恢复。"""
    goal = _goal("g1", "失败后恢复")
    assert goal.transition(GoalStatus.FAILED) is True
    assert goal.is_terminal is True
    assert goal.transition(GoalStatus.IN_PROGRESS) is True
    assert goal.is_terminal is False


def test_illegal_transition_keeps_state():
    """非法迁移返回 False 且不改动任何字段。"""
    goal = _goal("g1", "非法迁移保护")
    before = (goal.status, goal.progress, goal.updated_at)
    assert goal.can_transition_to(GoalStatus.COMPLETED) is False
    assert goal.transition(GoalStatus.COMPLETED) is False
    assert (goal.status, goal.progress, goal.updated_at) == before

    assert goal.transition(GoalStatus.APPROVED) is True
    assert goal.transition(GoalStatus.IN_PROGRESS) is True
    assert goal.transition(GoalStatus.COMPLETED) is True
    snapshot = (goal.status, goal.progress, goal.updated_at)
    assert goal.transition(GoalStatus.IN_PROGRESS) is False
    assert goal.transition(GoalStatus.APPROVED) is False
    assert (goal.status, goal.progress, goal.updated_at) == snapshot


def test_recompute_progress_from_sub_goals():
    """有子目标取均值；无子目标保值；已完成保持 1.0。"""
    parent = _goal("p", "父目标")
    child_a = parent.add_sub_goal(_goal("c1", "子目标一", progress=0.5))
    parent.add_sub_goal(_goal("c2", "子目标二", progress=1.0))
    assert child_a.parent_id == "p"
    assert parent.recompute_progress() == pytest.approx(0.75)

    solo = _goal("s", "无子目标", progress=0.3)
    assert solo.recompute_progress() == pytest.approx(0.3)

    done = Goal(id="p2", description="已完成", status=GoalStatus.COMPLETED,
                progress=1.0, sub_goals=[_goal("c3", "子目标三", progress=0.1)])
    assert done.recompute_progress() == 1.0


def test_to_dict_from_dict_roundtrip():
    """递归序列化 / 反序列化保持结构。"""
    parent = Goal(id="p", description="父目标", status=GoalStatus.IN_PROGRESS,
                  progress=0.4, metrics={"steps": 2}, metadata={"owner": "swe"})
    parent.add_sub_goal(Goal(id="c1", description="子目标一",
                             status=GoalStatus.COMPLETED, progress=1.0))
    data = parent.to_dict()

    restored = Goal.from_dict(data)
    assert isinstance(restored, Goal)
    assert restored.to_dict() == data
    assert restored.status is GoalStatus.IN_PROGRESS
    assert restored.metrics == {"steps": 2}
    assert restored.metadata == {"owner": "swe"}
    assert [item.id for item in restored.sub_goals] == ["c1"]
    assert restored.sub_goals[0].status is GoalStatus.COMPLETED
    assert restored.sub_goals[0].parent_id == "p"


def test_from_dict_tolerates_missing_and_invalid_fields():
    """缺失字段取默认值，非法状态回退 PENDING。"""
    fallback = Goal.from_dict({"id": "x", "status": "bogus", "progress": "nan!"})
    assert fallback.status is GoalStatus.PENDING
    assert fallback.description == ""
    assert fallback.progress == 0.0
    assert fallback.sub_goals == []
    assert fallback.metadata == {}

    empty = Goal.from_dict({})
    assert empty.id == ""
    assert empty.status is GoalStatus.PENDING


def test_goal_prompt_block_contains_goal_and_progress():
    """Prompt 块以 [当前目标] 开头并包含描述与进度。"""
    goal = Goal(id="g", description="实现目标驱动", status=GoalStatus.IN_PROGRESS,
                progress=0.4)
    block = goal.goal_prompt_block()
    assert block.startswith("[当前目标]")
    assert "实现目标驱动" in block
    assert "进行中" in block
    assert "40%" in block
    assert "子目标" not in block

    goal.add_sub_goal(Goal(id="c1", description="子目标A"))
    goal.add_sub_goal(Goal(id="c2", description="子目标B",
                           status=GoalStatus.COMPLETED, progress=1.0))
    block2 = goal.goal_prompt_block()
    assert block2.startswith("[当前目标]")
    assert "- 子目标:" in block2
    assert "[pending] 子目标A" in block2
    assert "[completed] 子目标B" in block2


def test_to_tasks_with_sub_goals_skips_terminal():
    """有子目标时只为非终态子目标生成任务，并保持顺序与优先级。"""
    parent = _goal("p", "父目标")
    parent.add_sub_goal(_goal("c1", "子目标一"))
    parent.add_sub_goal(_goal("c2", "子目标二",
                              status=GoalStatus.COMPLETED, progress=1.0))
    parent.add_sub_goal(_goal("c3", "子目标三", status=GoalStatus.FAILED))
    parent.add_sub_goal(_goal("c4", "子目标四", status=GoalStatus.IN_PROGRESS))

    tasks = parent.to_tasks()
    assert len(tasks) == 2
    assert [task.id for task in tasks] == ["c1", "c4"]
    assert [task.instruction for task in tasks] == ["子目标一", "子目标四"]
    assert [task.priority for task in tasks] == [0, -1]
    assert all(task.status is TaskStatus.IDLE for task in tasks)
    assert all(task.criticality == "critical" for task in tasks)
    assert all(task.dependencies == [] for task in tasks)
    assert all(task.metadata == {"goal_id": "p", "goal_sub": True} for task in tasks)


def test_to_tasks_without_sub_goals_single_task():
    """无子目标时生成单个任务。"""
    goal = _goal("s", "单目标")
    tasks = goal.to_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == "s"
    assert tasks[0].instruction == "单目标"
    assert tasks[0].status is TaskStatus.IDLE
    assert tasks[0].criticality == "critical"
    assert tasks[0].metadata == {"goal_id": "s", "goal_sub": False}


def test_goal_store_persists_and_reloads(ws_tmp):
    """写入 -> close -> 同 path 重新打开可读回，落盘为原子写。"""
    path = ws_tmp / "goals.json"
    store = GoalStore(path=str(path))
    store.upsert(Goal(id="g1", description="目标一",
                      status=GoalStatus.IN_PROGRESS, progress=0.5))
    parent = Goal(id="g2", description="目标二")
    parent.add_sub_goal(Goal(id="g2a", description="子目标"))
    store.upsert(parent)

    assert store.get("g1").description == "目标一"
    assert len(store.list()) == 2
    assert [goal.id for goal in store.list(status=GoalStatus.IN_PROGRESS)] == ["g1"]
    assert [goal.id for goal in store.active()] == ["g1", "g2"]
    summary = store.progress_summary()
    assert summary["total"] == 2
    assert summary["active"] == 2
    assert summary["completed"] == 0
    assert summary["failed"] == 0
    assert summary["avg_progress"] == pytest.approx(0.25)
    store.close()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert sorted(raw["goals"]) == ["g1", "g2"]
    assert not (ws_tmp / "goals.json.tmp").exists()

    reopened = GoalStore(path=str(path))
    loaded = reopened.get("g2")
    assert loaded is not None
    assert [item.id for item in loaded.sub_goals] == ["g2a"]
    assert loaded.sub_goals[0].parent_id == "g2"
    assert reopened.get("g1").progress == pytest.approx(0.5)
    assert reopened.remove("g1") is True
    assert reopened.remove("g1") is False
    assert [goal.id for goal in reopened.list()] == ["g2"]
    reopened.close()


def test_goal_store_corrupt_file_falls_back(ws_tmp):
    """损坏 / 结构非法的 JSON 文件回退为空台账且不抛异常。"""
    broken = ws_tmp / "broken.json"
    broken.write_text("{ 这不是合法 JSON", encoding="utf-8")
    store = GoalStore(path=str(broken))
    assert store.list() == []
    assert store.get("x") is None
    assert store.active() == []
    assert store.progress_summary()["total"] == 0
    store.upsert(Goal(id="g", description="恢复写入"))
    assert store.get("g") is not None
    store.close()

    bad_shape = ws_tmp / "bad_shape.json"
    bad_shape.write_text(json.dumps({"version": 1, "goals": [1, 2, 3]}),
                         encoding="utf-8")
    store2 = GoalStore(path=str(bad_shape))
    assert store2.list() == []
    store2.close()


def test_goal_store_disabled_is_memory_only(ws_tmp):
    """enabled=False 或未给 path 时绝不写盘。"""
    path = ws_tmp / "memory_only.json"
    store = GoalStore(path=str(path), enabled=False)
    store.upsert(Goal(id="g", description="仅内存"))
    store.close()
    assert not path.exists()
    assert not (ws_tmp / "memory_only.json.tmp").exists()
    assert store.get("g") is not None

    memory = GoalStore()
    memory.upsert(Goal(id="m", description="无路径"))
    assert memory.get("m") is not None
    assert memory.progress_summary()["total"] == 1
    memory.close()
