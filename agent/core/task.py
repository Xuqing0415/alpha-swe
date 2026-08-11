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
    RETRYING = "retrying"  # 失败后等待重试（方案 1.1）
    SKIPPED = "skipped"    # 非关键步骤失败后跳过（方案 1.2）
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
    role: str = ""  # 多 Agent 协作：coder / reviewer / tester ...
    parent_id: Optional[str] = None
    # 任务级重试（方案 1.1）：max_retries 默认 3，策略 immediate/backoff/retry_with_context
    max_retries: int = 3
    retry_count: int = 0
    retry_strategy: str = "backoff"
    # 步骤级降级（方案 1.2）：critical 失败上抛 / normal 标记 SKIPPED / optional 静默跳过。
    # 默认 critical：未显式声明的任务失败即会话失败（跳过必须由规划器显式授权）。
    criticality: str = "critical"
    result: Any = None
    error: Optional[str] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)  # 技能步骤决策点等附加信息
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
            "role": self.role,
            "parent_id": self.parent_id,
            "max_retries": self.max_retries,
            "retry_count": self.retry_count,
            "retry_strategy": self.retry_strategy,
            "criticality": self.criticality,
            "result": self.result,
            "error": self.error,
            "metadata": dict(self.metadata),
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
        role: str = "",
    ) -> Task:
        tid = task_id or uuid.uuid4().hex[:8]
        task = Task(
            id=tid,
            instruction=instruction,
            dependencies=list(dependencies or []),
            priority=priority,
            parent_id=parent_id,
            role=role,
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
            # SKIPPED 视为已完成：非关键步骤被跳过不阻塞后续步骤（方案 1.2）
            if dep.status not in (TaskStatus.COMPLETED, TaskStatus.SKIPPED):
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
            t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                             TaskStatus.SKIPPED)
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

    # ---- 快照（方案 1.3 断点续跑） ----
    def to_snapshot(self) -> Dict[str, Any]:
        """序列化整个 DAG（含状态/结果/重试/降级字段，不含完整 history）。"""
        return {
            "version": 1,
            "created_at": datetime.now().isoformat(),
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }

    @classmethod
    def from_snapshot(cls, data: Dict[str, Any]) -> "TaskDAG":
        """从快照恢复 DAG；字段缺失时用保守默认值。"""
        dag = cls()
        for item in data.get("tasks", []):
            task = Task(
                id=item.get("id", uuid.uuid4().hex[:8]),
                instruction=item.get("instruction", ""),
                status=TaskStatus(item.get("status", "idle")),
                dependencies=list(item.get("dependencies", [])),
                priority=item.get("priority", 0),
                role=item.get("role", ""),
                parent_id=item.get("parent_id"),
                max_retries=item.get("max_retries", 3),
                retry_count=item.get("retry_count", 0),
                retry_strategy=item.get("retry_strategy", "backoff"),
                criticality=item.get("criticality", "critical"),
                result=item.get("result"),
                error=item.get("error"),
                metadata=dict(item.get("metadata", {})),
                round_count=item.get("round_count", 0),
            )
            dag.add(task)
        return dag

