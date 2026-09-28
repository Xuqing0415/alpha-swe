# -*- coding: utf-8 -*-
"""Goal（目标驱动）基础模块 —— 目标建模、生命周期与任务 / 提示词转换。

定位：目标驱动能力的分层落点，链路为
Goal（目标）-> Task（任务）-> 进度追踪 -> Prompt 注入。

本模块只提供数据模型与转换能力，暂不接线到 AgentLoop：
- GoalStatus：目标生命周期状态与合法迁移表；
- Goal：目标数据模型（受控状态迁移、子目标树、进度推导、序列化、Prompt 注入）；
- GoalStore：目标台账，可选 JSON 原子持久化（不落盘时纯内存）。

后续接线方式：Goal.to_tasks() 的产物正是 AgentLoop Planner 接口
async def plan(self, prompt, context="") -> List[Task] 所需的 List[Task]；
Goal.goal_prompt_block() 的产物用于注入 System Prompt。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # 仅用于类型标注：运行时不导入，避免与 core.task 循环依赖
    from agent.core.task import Task

logger = logging.getLogger("alpha-swe.goal")


class GoalStatus(str, Enum):
    """目标状态：待批准 -> 已批准 -> 进行中 -> 已完成 / 已失败。"""

    PENDING = "pending"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


# 合法状态迁移表：完成后为终态，不再允许迁出。
_ALLOWED_TRANSITIONS: Dict[GoalStatus, frozenset] = {
    GoalStatus.PENDING: frozenset({GoalStatus.APPROVED, GoalStatus.FAILED}),
    GoalStatus.APPROVED: frozenset({GoalStatus.IN_PROGRESS, GoalStatus.FAILED}),
    GoalStatus.IN_PROGRESS: frozenset({GoalStatus.COMPLETED, GoalStatus.FAILED}),
    GoalStatus.FAILED: frozenset({GoalStatus.IN_PROGRESS}),
    GoalStatus.COMPLETED: frozenset(),
}

# 终态集合：到达后目标不再推进。
_TERMINAL_STATUSES = frozenset({GoalStatus.COMPLETED, GoalStatus.FAILED})

# 状态 -> 中文展示词（用于 Prompt 注入）。
_STATUS_LABELS: Dict[GoalStatus, str] = {
    GoalStatus.PENDING: "待批准",
    GoalStatus.APPROVED: "已批准",
    GoalStatus.IN_PROGRESS: "进行中",
    GoalStatus.COMPLETED: "已完成",
    GoalStatus.FAILED: "已失败",
}


def _coerce_status(value: Any) -> Optional[GoalStatus]:
    """把任意输入宽松转换为 GoalStatus；无法识别时返回 None。"""
    if isinstance(value, GoalStatus):
        return value
    try:
        return GoalStatus(str(value))
    except ValueError:
        return None


@dataclass
class Goal:
    """一个可追踪的目标（可含子目标树）。"""

    id: str
    description: str
    status: GoalStatus = GoalStatus.PENDING
    progress: float = 0.0
    sub_goals: List["Goal"] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def __post_init__(self) -> None:
        """把宽松传入的状态（如字符串）归一为 GoalStatus。"""
        if not isinstance(self.status, GoalStatus):
            self.status = _coerce_status(self.status) or GoalStatus.PENDING

    # ---- 状态机 ----
    @property
    def is_terminal(self) -> bool:
        """是否处于终态（已完成 / 已失败）。"""
        return self.status in _TERMINAL_STATUSES

    def can_transition_to(self, status: GoalStatus) -> bool:
        """判断能否迁移到目标状态（不修改任何字段）。"""
        target = _coerce_status(status)
        if target is None:
            return False
        return target in _ALLOWED_TRANSITIONS.get(self.status, frozenset())

    def transition(self, status: GoalStatus) -> bool:
        """执行状态迁移；非法迁移返回 False 且不改动任何字段。"""
        target = _coerce_status(status)
        if target is None or not self.can_transition_to(target):
            return False
        self.status = target
        self.updated_at = datetime.now().isoformat()
        if target == GoalStatus.COMPLETED:
            self.progress = 1.0
        return True

    # ---- 子目标与进度 ----
    def add_sub_goal(self, goal: "Goal") -> "Goal":
        """挂载子目标，建立父子关系并返回该子目标。"""
        goal.parent_id = self.id
        self.sub_goals.append(goal)
        self.updated_at = datetime.now().isoformat()
        return goal

    def recompute_progress(self) -> float:
        """推导自身进度：有子目标取子目标均值，否则保持原值。"""
        if self.status == GoalStatus.COMPLETED:
            self.progress = 1.0
            return self.progress
        if self.sub_goals:
            values = [float(goal.progress) for goal in self.sub_goals]
            self.progress = round(sum(values) / len(values), 4)
        return self.progress

    # ---- 序列化 ----
    def to_dict(self) -> Dict[str, Any]:
        """递归序列化（含子目标）。"""
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status.value,
            "progress": self.progress,
            "sub_goals": [goal.to_dict() for goal in self.sub_goals],
            "metrics": dict(self.metrics),
            "parent_id": self.parent_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Goal":
        """从字典递归反序列化；缺失字段取保守默认值，非法状态回退 PENDING。"""
        if not isinstance(data, dict):
            data = {}
        status = _coerce_status(data.get("status")) or GoalStatus.PENDING
        try:
            progress = float(data.get("progress", 0.0))
        except (TypeError, ValueError):
            progress = 0.0
        sub_raw = data.get("sub_goals")
        sub_goals: List["Goal"] = []
        if isinstance(sub_raw, list):
            sub_goals = [cls.from_dict(item) for item in sub_raw]
        metrics = data.get("metrics")
        metadata = data.get("metadata")
        now = datetime.now().isoformat()
        return cls(
            id=str(data.get("id", "")),
            description=str(data.get("description", "")),
            status=status,
            progress=progress,
            sub_goals=sub_goals,
            metrics=dict(metrics) if isinstance(metrics, dict) else {},
            parent_id=data.get("parent_id"),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
            created_at=str(data.get("created_at") or now),
            updated_at=str(data.get("updated_at") or now),
        )

    # ---- Prompt 注入 ----
    def goal_prompt_block(self) -> str:
        """生成注入 System Prompt 的目标文本块（始终非空）。"""
        percent = int(round(float(self.progress) * 100))
        lines = [
            "[当前目标]",
            "- 目标: {0}".format(self.description),
            "- 状态: {0}，进度 {1}%".format(_STATUS_LABELS[self.status], percent),
        ]
        if self.sub_goals:
            lines.append("- 子目标:")
            for goal in self.sub_goals:
                lines.append("  - [{0}] {1}".format(goal.status.value, goal.description))
        return "\n".join(lines)

    # ---- 目标 -> 任务 ----
    def to_tasks(self) -> List[Task]:
        """把目标转换为 Task 列表，供 Planner 接口消费。"""
        from agent.core.task import Task

        tasks: List[Task] = []
        if self.sub_goals:
            index = 0
            for goal in self.sub_goals:
                if goal.is_terminal:
                    continue
                tasks.append(Task(
                    id=goal.id,
                    instruction=goal.description,
                    priority=-index,
                    metadata={"goal_id": self.id, "goal_sub": True},
                ))
                index += 1
        else:
            tasks.append(Task(
                id=self.id,
                instruction=self.description,
                metadata={"goal_id": self.id, "goal_sub": False},
            ))
        return tasks


class GoalStore:
    """目标台账：可选 JSON 原子持久化，未启用时纯内存。"""

    VERSION = 1

    def __init__(self, path: Optional[str] = None, enabled: bool = True) -> None:
        self._path = path
        self._persistent = bool(path) and bool(enabled)
        self._closed = False
        self._goals: Dict[str, Goal] = self._load()

    # ---- 持久化 ----
    def _load(self) -> Dict[str, Goal]:
        """读取台账；文件缺失 / 损坏 / 结构非法时回退为空字典，绝不抛异常。"""
        if not self._persistent or not self._path:
            return {}
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning("目标台账读取失败，回退为空台账: %s", exc)
            return {}
        if not isinstance(raw, dict):
            logger.warning("目标台账结构非法（顶层非对象），回退为空台账")
            return {}
        goals_raw = raw.get("goals")
        if not isinstance(goals_raw, dict):
            logger.warning("目标台账结构非法（goals 非对象），回退为空台账")
            return {}
        goals: Dict[str, Goal] = {}
        for goal_id, item in goals_raw.items():
            if not isinstance(item, dict):
                logger.warning("目标台账条目非法，已跳过: %s", goal_id)
                continue
            goals[str(goal_id)] = Goal.from_dict(item)
        return goals

    def _persist(self) -> None:
        """原子落盘：先写 <path>.tmp 再 os.replace；失败只记警告。"""
        if not self._persistent or not self._path:
            return
        payload = {
            "version": self.VERSION,
            "goals": {goal_id: goal.to_dict() for goal_id, goal in self._goals.items()},
        }
        tmp_path = self._path + ".tmp"
        try:
            parent = os.path.dirname(os.path.abspath(self._path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("目标台账落盘失败（已忽略）: %s", exc)
            return
        try:
            os.replace(tmp_path, self._path)
        except OSError as exc:
            logger.warning("目标台账原子替换失败（已忽略）: %s", exc)

    # ---- 增删查 ----
    def upsert(self, goal: Goal) -> Goal:
        """写入 / 覆盖目标。"""
        self._goals[goal.id] = goal
        self._persist()
        return goal

    def get(self, goal_id: str) -> Optional[Goal]:
        """按 id 读取目标。"""
        return self._goals.get(goal_id)

    def list(self, status: Optional[GoalStatus] = None) -> List[Goal]:
        """列出目标，可按状态过滤。"""
        if status is None:
            return list(self._goals.values())
        target = _coerce_status(status)
        if target is None:
            return []
        return [goal for goal in self._goals.values() if goal.status == target]

    def remove(self, goal_id: str) -> bool:
        """删除目标，返回是否删除了记录。"""
        if goal_id not in self._goals:
            return False
        del self._goals[goal_id]
        self._persist()
        return True

    def active(self) -> List[Goal]:
        """列出非终态目标。"""
        return [goal for goal in self._goals.values() if not goal.is_terminal]

    def progress_summary(self) -> Dict[str, Any]:
        """汇总台账进度：总数 / 活跃 / 完成 / 失败 / 平均进度。"""
        goals = list(self._goals.values())
        total = len(goals)
        completed = sum(1 for goal in goals if goal.status == GoalStatus.COMPLETED)
        failed = sum(1 for goal in goals if goal.status == GoalStatus.FAILED)
        active = sum(1 for goal in goals if not goal.is_terminal)
        if total:
            avg = round(sum(float(goal.progress) for goal in goals) / total, 4)
        else:
            avg = 0.0
        return {
            "total": total,
            "active": active,
            "completed": completed,
            "failed": failed,
            "avg_progress": avg,
        }

    def close(self) -> None:
        """收尾：确保内存变更已落盘（幂等）。"""
        if self._closed:
            return
        self._persist()
        self._closed = True
