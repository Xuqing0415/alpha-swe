"""多 Agent 消息协议 —— 对应设计第 8 节。

内部消息 `{sender, receiver, type, payload}`，支持 TASK_ASSIGN / TASK_RESULT /
QUERY / REVIEW_REQUEST / REVIEW_RESULT 等类型。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict


class MsgType(str, Enum):
    TASK_ASSIGN = "task_assign"          # orchestrator -> worker
    TASK_RESULT = "task_result"          # worker -> orchestrator
    QUERY = "query"                      # 任意 -> 任意（索取信息）
    QUERY_RESULT = "query_result"
    REVIEW_REQUEST = "review_request"    # orchestrator -> reviewer
    REVIEW_RESULT = "review_result"      # reviewer -> orchestrator
    RETRY = "retry"                      # orchestrator -> worker（带反馈）
    DONE = "done"                        # orchestrator -> 团队会话结束
    ERROR = "error"


@dataclass
class Message:
    """团队内消息。"""
    sender: str
    receiver: str
    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    ts: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "type": self.type,
            "payload": self.payload,
            "id": self.id,
            "ts": self.ts,
        }


__all__ = ["Message", "MsgType"]
