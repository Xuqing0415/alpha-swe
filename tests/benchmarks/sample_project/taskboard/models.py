"""任务数据模型：Task 数据类 + 优先级/状态枚举。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def parse(cls, value: str) -> "Priority":
        """宽松解析优先级字符串（大小写不敏感），非法值抛 ValueError。"""
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            raise ValueError(
                f"非法优先级: {value!r}（可选: low/medium/high/critical）"
            ) from None


class Status(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"

    @classmethod
    def parse(cls, value: str) -> "Status":
        try:
            return cls(str(value).strip().lower().replace(" ", "_"))
        except ValueError:
            raise ValueError(
                f"非法状态: {value!r}（可选: todo/in_progress/done）"
            ) from None


@dataclass
class Task:
    """一条看板任务。

    - ``total_estimate``：预估总工作量（单位：小时），用于统计与排期。
    - ``spent``：已投入工作量（分钟），完成时由 ``complete()`` 写入。
    """
    title: str
    priority: Priority = Priority.MEDIUM
    status: Status = Status.TODO
    tags: List[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    total_estimate: int = 0
    spent: int = 0

    def complete(self, spent: int = 0) -> None:
        """标记任务完成并记录实际耗时（分钟）。"""
        self.status = Status.DONE
        self.spent = int(spent)
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    def is_done(self) -> bool:
        return self.status == Status.DONE

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "priority": self.priority.value,
            "status": self.status.value,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_estimate": self.total_estimate,
            "spent": self.spent,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(
            title=str(data.get("title", "")),
            priority=Priority.parse(data.get("priority", "medium")),
            status=Status.parse(data.get("status", "todo")),
            tags=[str(t) for t in data.get("tags", [])],
            id=str(data.get("id", uuid.uuid4().hex[:12])),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            total_estimate=int(data.get("total_estimate", 0) or 0),
            spent=int(data.get("spent", 0) or 0),
        )
