# -*- coding: utf-8 -*-
"""统一事件流 schema —— TUI / Web / SSE / CLI 共用的事件类型枚举、分组与规范化。

本模块只提供能力，不在任何链路上接线：

- :class:`EventKind` 给出事件分组枚举；
- :data:`EVENT_TYPES` 登记当前代码中已知的事件类型，未知类型不报错、归入
  :attr:`EventKind.OTHER`；
- :func:`normalize_event` 把任意输入整理成 ``{"type", "data", "ts"}`` 形状的副本，
  绝不修改入参，便于 SSE / Web / TUI 各自消费同一份事件记录而不互相干扰；
- :func:`validate_event` / :func:`is_valid` 用于自检与灰度对账；
- :func:`to_sse` 与 ``server/events.py::sse_generator`` 保持完全一致的文本格式；
- :func:`describe` 提供单行人类可读摘要，供 TUI / CLI 直接打印。

事件记录形状与 ``agent/core/loop.py::_emit`` 一致::

    {"type": <str>, "data": {**kwargs}, "ts": time.time()}
"""
from __future__ import annotations

import json
import numbers
import time
from enum import Enum
from typing import Any, Dict, List, Optional


class EventKind(str, Enum):
    """事件分组：让 TUI / Web 可以按大类过滤或着色，而不必穷举具体类型。"""

    LIFECYCLE = "lifecycle"   # 运行生命周期（开始 / 计划 / 结束 / 中断 / 优先级）
    TASK = "task"             # 任务粒度事件（完成 / 失败 / 重试 / 跳过 / 抢占）
    THOUGHT = "thought"       # 模型思考
    TOOL = "tool"             # 工具调用
    STATE = "state"           # 会话状态快照
    BUDGET = "budget"         # 预算告警 / 耗尽
    BARRIER = "barrier"       # 阶段栅栏
    ERROR = "error"           # 错误（run_error 归入 LIFECYCLE，此处保留给未来细分）
    OTHER = "other"           # 未知类型兜底


# 已知事件类型 -> 分组。新增事件类型时在此登记即可，未知类型不会抛错。
EVENT_TYPES: Dict[str, EventKind] = {
    # 运行生命周期
    "run_start": EventKind.LIFECYCLE,
    "plan_created": EventKind.LIFECYCLE,
    "dry_run_plan": EventKind.LIFECYCLE,
    "run_done": EventKind.LIFECYCLE,
    "run_error": EventKind.LIFECYCLE,
    "interrupt": EventKind.LIFECYCLE,
    "priority_changed": EventKind.LIFECYCLE,
    # 任务粒度
    "task_done": EventKind.TASK,
    "task_failed": EventKind.TASK,
    "task_retry": EventKind.TASK,
    "task_skipped": EventKind.TASK,
    "task_resumed": EventKind.TASK,
    "task_preempted": EventKind.TASK,
    "task_interrupted": EventKind.TASK,
    # 思考与工具
    "think": EventKind.THOUGHT,
    "tool_call": EventKind.TOOL,
    # 预算
    "budget_warning": EventKind.BUDGET,
    "budget_exhausted": EventKind.BUDGET,
    # 阶段栅栏
    "phase_barrier_task_start": EventKind.BARRIER,
    "phase_barrier_summary": EventKind.BARRIER,
    # 会话状态
    "session_state": EventKind.STATE,
}

# 一条规范事件记录必须具备的顶层键。
REQUIRED_FIELDS = ("type", "data", "ts")

# describe 摘要最多展示的 data 键数量与单个值的最大长度。
_MAX_DESC_KEYS = 3
_MAX_DESC_VALUE = 40


def kind_of(event_type: str) -> EventKind:
    """返回事件类型所属分组；未知类型（含非字符串）返回 :attr:`EventKind.OTHER`。"""
    if not isinstance(event_type, str):
        return EventKind.OTHER
    return EVENT_TYPES.get(event_type, EventKind.OTHER)


def _is_number(value: Any) -> bool:
    """数值判定：排除 bool（bool 是 int 子类，但语义上不是时间戳）。"""
    if isinstance(value, bool):
        return False
    return isinstance(value, numbers.Real)


def normalize_event(record: Any) -> Dict[str, Any]:
    """把任意输入整理成规范事件记录，返回新副本，绝不修改入参。

    - 非 dict 入参视为空记录；
    - ``type`` 缺失 / 非字符串 / 空串 -> ``"unknown"``；
    - ``data`` 缺失 -> ``{}``；非 dict -> ``{"value": <原值>}``；
    - ``ts`` 缺失 / 非数值 -> 当前时间 ``time.time()``；
    - 已知顶层键之外的额外键原样保留。
    """
    event: Dict[str, Any] = dict(record) if isinstance(record, dict) else {}

    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        event["type"] = "unknown"

    if "data" not in event:
        event["data"] = {}
    elif not isinstance(event["data"], dict):
        event["data"] = {"value": event["data"]}
    else:
        # 浅拷贝一层，避免调用方改写返回副本时意外污染入参的 data。
        event["data"] = dict(event["data"])

    if "ts" not in event or not _is_number(event["ts"]):
        event["ts"] = time.time()

    return event


def validate_event(record: Any) -> List[str]:
    """校验事件记录，返回问题描述列表；空列表表示结构合法。

    结构性错误包括：非 dict、缺 ``type`` / ``data`` / ``ts``、字段类型不符。
    未知事件类型只给出一条提示性字符串，不属于结构性错误，但同样计入返回列表。
    """
    if not isinstance(record, dict):
        return ["事件记录必须是 dict，实际为 %s" % type(record).__name__]

    problems: List[str] = []

    if "type" not in record:
        problems.append("缺少字段: type")
    else:
        event_type = record["type"]
        if not isinstance(event_type, str):
            problems.append("字段 type 必须是字符串，实际为 %s"
                            % type(event_type).__name__)
        elif not event_type.strip():
            problems.append("字段 type 不能为空字符串")

    if "data" not in record:
        problems.append("缺少字段: data")
    elif not isinstance(record["data"], dict):
        problems.append("字段 data 必须是 dict，实际为 %s"
                        % type(record["data"]).__name__)

    if "ts" not in record:
        problems.append("缺少字段: ts")
    elif not _is_number(record["ts"]):
        problems.append("字段 ts 必须是数值，实际为 %s"
                        % type(record["ts"]).__name__)

    event_type = record.get("type")
    if isinstance(event_type, str) and event_type.strip() \
            and event_type not in EVENT_TYPES:
        problems.append("未知事件类型: %s" % event_type)

    return problems


def is_valid(record: Any) -> bool:
    """``validate_event`` 无任何问题（含未知类型提示）时返回 True。"""
    return not validate_event(record)


def to_sse(record: Any, event_name: Optional[str] = None) -> str:
    """序列化为 SSE 文本，与 ``server/events.py::sse_generator`` 格式一致。

    正常输出 ``event: <name>\\ndata: <json>\\n\\n``；``ping`` 特例输出 ``": ping\\n\\n"``。
    ``event_name`` 非空时覆盖最终事件名；data 不可 JSON 序列化时回退 ``default=str``。
    """
    event = normalize_event(record)
    name = event_name if isinstance(event_name, str) and event_name else event["type"]
    if name == "ping":
        return ": ping\n\n"
    payload = json.dumps(event["data"], ensure_ascii=False, default=str)
    return "event: %s\ndata: %s\n\n" % (name, payload)


def _clean_text(text: str) -> str:
    """压掉换行 / 制表符等，保证摘要严格单行。"""
    return " ".join(text.replace("\r", " ").replace("\n", " ")
                    .replace("\t", " ").split())


def describe(record: Any) -> str:
    """单行人类可读摘要，例如 ``tool_call(task_id=t0, tool=file_ops)``。

    取 ``data`` 中前若干键值；无 data 时回退为类型名；未知类型同样能输出。
    """
    event = normalize_event(record)
    event_type = event["type"]
    data = event["data"]

    parts: List[str] = []
    for key in list(data)[:_MAX_DESC_KEYS]:
        value = _clean_text(str(data[key]))
        if len(value) > _MAX_DESC_VALUE:
            value = value[:_MAX_DESC_VALUE] + "..."
        parts.append("%s=%s" % (key, value))

    if not parts:
        return event_type
    return "%s(%s)" % (event_type, ", ".join(parts))


def known_types() -> List[str]:
    """返回排序后的已知事件类型列表。"""
    return sorted(EVENT_TYPES)


__all__ = [
    "EventKind",
    "EVENT_TYPES",
    "REQUIRED_FIELDS",
    "kind_of",
    "normalize_event",
    "validate_event",
    "is_valid",
    "to_sse",
    "describe",
    "known_types",
]
