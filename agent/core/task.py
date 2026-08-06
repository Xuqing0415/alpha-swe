"""任务表示与 DAG —— 对应设计第 3.1 节。

Task 支持依赖（dependencies）、优先级（priority）与递归拆分（parent_id）。
TaskDAG 负责维护任务集合、依赖就绪判定与就绪任务排序。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskStatus(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    """单个可调度任务。"""
    id: str
    instruction: str
    status: TaskStatus = TaskStatus.IDLE
    dependencies: List[str] = field(default_factory=list)
    priority: int = 0
    parent_id: Optional[str] = None
    result: Any = None
    error: Optional[str] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    round_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat()

    def mark(self, status: TaskStatus, result: Any = None, error: Optional[str] = None) -> None:
        self.status = status
        if result is not None:
            self.result = result
        if error is not None:
            self.error = error
        self.touch()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "instruction": self.instruction,
            "status": self.status.value,
            "dependencies": list(self.dependencies),
            "priority": self.priority,
            "parent_id": self.parent_id,
            "result": self.result,
            "error": self.error,
            "round_count": self.round_count,
        }


class TaskDAG:
    """任务有向无环图：增删任务、就绪判定、排序。"""

    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}

    # ---- 增删 ----
    def add(self, task: Task) -> Task:
        self._tasks[task.id] = task
        return task

    def create_task(
        self,
        instruction: str,
        dependencies: Optional[List[str]] = None,
        priority: int = 0,
        parent_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Task:
        tid = task_id or uuid.uuid4().hex[:8]
        task = Task(
            id=tid,
            instruction=instruction,
            dependencies=list(dependencies or []),
            priority=priority,
            parent_id=parent_id,
        )
        return self.add(task)

    def remove(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def all(self) -> List[Task]:
        return list(self._tasks.values())

    # ---- 查询 ----
    def dependencies_satisfied(self, task: Task) -> bool:
        for dep_id in task.dependencies:
            dep = self._tasks.get(dep_id)
            if dep is None:
                continue  # 缺失依赖按满足处理（宽松）
            if dep.status != TaskStatus.COMPLETED:
                return False
        return True

    def ready_tasks(self) -> List[Task]:
        """依赖全部满足且为 READY 的任务，按优先级降序、创建序升序。"""
        ready = [
            t for t in self._tasks.values()
            if t.status == TaskStatus.READY and self.dependencies_satisfied(t)
        ]
        return sorted(ready, key=lambda t: (-t.priority, t.created_at))

    def pending(self) -> bool:
        """是否仍有未终结的任务。"""
        return any(
            t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            for t in self._tasks.values()
        )

    def has_waiting(self) -> bool:
        return any(t.status == TaskStatus.WAITING for t in self._tasks.values())

    def children(self, parent_id: str) -> List[Task]:
        return [t for t in self._tasks.values() if t.parent_id == parent_id]

    def mark(self, task_id: str, status: TaskStatus,
             result: Any = None, error: Optional[str] = None) -> Optional[Task]:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        task.mark(status, result=result, error=error)
        return task

    def promote_dependents(self, task_id: str) -> List[Task]:
        """任务完成后，把依赖它的任务提升为 READY。"""
        promoted: List[Task] = []
        for task in self._tasks.values():
            if (
                task_id in task.dependencies
                and task.status == TaskStatus.IDLE
                and self.dependencies_satisfied(task)
            ):
                task.mark(TaskStatus.READY)
                promoted.append(task)
        return promoted

    def summary(self) -> Dict[str, Any]:
        by_status: Dict[str, int] = {}
        for t in self._tasks.values():
            by_status[t.status.value] = by_status.get(t.status.value, 0) + 1
        return {"total": len(self._tasks), "by_status": by_status}