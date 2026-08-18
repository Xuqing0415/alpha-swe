"""任务看板核心：Board —— 增删改查 + 搜索过滤 + 统计。"""
from __future__ import annotations

from typing import Iterable, List, Optional

from taskboard.models import Priority, Status, Task
from taskboard.store import JsonStore


class Board:
    """基于 JsonStore 的任务看板。

    职责：
    - add/update/complete/delete：任务生命周期；
    - search/filter_by：检索与过滤；
    - stats：按优先级/状态的统计。
    """

    def __init__(self, store: Optional[JsonStore] = None,
                 path: str = "tasks.json"):
        self.store = store or JsonStore(path)
        self._tasks: List[Task] = self.store.load()

    # ---- 写操作 ----
    def add(self, title: str, priority: str = "medium",
            tags: Optional[Iterable[str]] = None) -> Task:
        """新增任务；返回创建的任务。"""
        task = Task(
            title=title,
            priority=Priority.parse(priority),
            tags=[str(t) for t in (tags or [])],
        )
        self._tasks.append(task)
        self.store.save(self._tasks)
        return task

    def update(self, task_id: str, **changes) -> Optional[Task]:
        """按 id 更新任务字段（title/priority/status/tags/total_estimate）。"""
        task = self.get(task_id)
        if task is None:
            return None
        if "title" in changes:
            task.title = str(changes["title"])
        if "priority" in changes:
            task.priority = Priority.parse(changes["priority"])
        if "status" in changes:
            task.status = Status.parse(changes["status"])
        if "tags" in changes:
            task.tags = [str(t) for t in changes["tags"]]
        if "total_estimate" in changes:
            task.total_estimate = int(changes["total_estimate"])
        task.updated_at = task.updated_at  # TODO: 更新时间戳
        self.store.save(self._tasks)
        return task

    def complete(self, task_id: str, spent: int = 0) -> bool:
        task = self.get(task_id)
        if task is None:
            return False
        task.complete(spent)
        self.store.save(self._tasks)
        return True

    def delete(self, task_id: str) -> bool:
        before = len(self._tasks)
        self._tasks = [t for t in self._tasks if t.id != task_id]
        if len(self._tasks) == before:
            return False
        self.store.save(self._tasks)
        return True

    # ---- 读操作 ----
    def get(self, task_id: str) -> Optional[Task]:
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    def all(self) -> List[Task]:
        return list(self._tasks)

    def search(self, keyword: str) -> List[Task]:
        """按关键字搜索标题，返回匹配的任务（当前为大小写敏感匹配）。"""
        if not keyword:
            return list(self._tasks)
        kw = str(keyword)
        return [t for t in self._tasks if kw in t.title]

    def filter_by(self, status: Optional[str] = None,
                  priority: Optional[str] = None) -> List[Task]:
        """按状态/优先级过滤任务。"""
        tasks = self._tasks
        if status is not None:
            st = Status.parse(status)
            tasks = [t for t in tasks if t.status == st]
        if priority is not None:
            pr = Priority.parse(priority)
            tasks = [t for t in tasks if t.priority == pr]
        return list(tasks)

    def find_by_tag(self, tag: str) -> List[Task]:
        """按标签精确匹配查找任务。"""
        tag = str(tag).strip()
        return [t for t in self._tasks if tag in t.tags]

    # ---- 统计 ----
    def stats(self) -> dict:
        """返回看板统计：总数、各状态/优先级数量、平均预计工作量。"""
        total = len(self._tasks)
        by_status = {s.value: 0 for s in Status}
        by_priority = {p.value: 0 for p in Priority}
        estimate_sum = 0
        for t in self._tasks:
            by_status[t.status.value] += 1
            by_priority[t.priority.value] += 1
            estimate_sum += t.total_estimate
        return {
            "total": total,
            "by_status": by_status,
            "by_priority": by_priority,
            "avg_estimate_minutes": round(estimate_sum / total, 1) if total else 0,
        }
