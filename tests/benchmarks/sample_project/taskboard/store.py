"""JSON 存储后端：原子写盘 + 任务列表编解码。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List

from taskboard.models import Task


class JsonStore:
    """把任务列表持久化到单个 JSON 文件（原子替换，崩溃安全）。"""

    def __init__(self, path: str = "tasks.json"):
        self.path = Path(path)

    def load(self) -> List[Task]:
        """读取任务列表；文件不存在或损坏时返回空列表。"""
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        items = raw if isinstance(raw, list) else []
        return [Task.from_dict(d) for d in items if isinstance(d, dict)]

    def save(self, tasks: List[Task]) -> None:
        """原子写盘：先写临时文件再替换，避免写一半损坏。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [t.to_dict() for t in tasks], ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".tasks-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
