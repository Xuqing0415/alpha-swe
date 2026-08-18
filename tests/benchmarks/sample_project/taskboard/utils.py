"""工具函数：slugify / 日期解析 / 任务过滤。"""
from __future__ import annotations

import re
import string  # noqa: F401  # TODO: 移除未使用的 import
from datetime import datetime
from typing import Iterable, List, Optional

from taskboard.models import Priority, Status, Task

_WS_RE = re.compile(r"\s+")


def slugify(text: str) -> str:
    """把标题转成 URL 友好的 slug（小写、空格转连字符）。"""
    l = _WS_RE.sub("-", str(text).strip().lower())
    return re.sub(r"[^a-z0-9\-]", "", l)


def parse_date(value: str) -> Optional[datetime]:
    """解析 ISO 日期或日期时间字符串；无法解析返回 None。"""
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_priority(value: str) -> Priority:
    """解析优先级字符串，非法值抛 ValueError（转发 models 语义）。"""
    return Priority.parse(value)


def filter_tasks(tasks: Iterable[Task],
                 status: Optional[str] = None,
                 priority: Optional[str] = None,
                 keyword: Optional[str] = None) -> List[Task]:
    """按状态/优先级/关键字组合过滤任务（优先级高于关键字时同时生效）。"""
    result = list(tasks)
    if status is not None:
        st = Status.parse(status)
        result = [t for t in result if t.status == st]
    if priority is not None:
        pr = Priority.parse(priority)
        result = [t for t in result if t.priority == pr]
    if keyword:
        kw = str(keyword)
        result = [t for t in result if kw in t.title]
    return result
