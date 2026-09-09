"""黑板（Shared Blackboard）—— 对应设计第 8 节。

内存共享状态：Worker 发布成果（diff / 测试报告 / 文件清单），Orchestrator 汇总。
接口与 Redis Pub/Sub 对齐：publish / get / subscribe，便于将来替换实现。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from agent.multiagent.messages import (Message, MsgType, USER_PRIORITY,
                              USER_SENDER)

logger = logging.getLogger("alpha-swe.multiagent.blackboard")

Artifact = Dict[str, Any]


class Blackboard:
    """线程/协程安全的内存黑板（单事件循环内同步访问）。"""

    def __init__(self) -> None:
        self._artifacts: Dict[str, Artifact] = {}
        self._messages: List[Message] = []
        self._subscribers: Dict[str, List[Callable[[Artifact], None]]] = {}
        # 主线二 2.1A：文件级写锁——同一文件同一时刻只允许一个 Agent 写入
        self._file_locks: Dict[str, Dict[str, Any]] = {}
        # 主线二 2.3：用户超级角色消息通道（独立于普通团队消息日志）
        self._user_messages: List[Message] = []
        self._user_cursor: Dict[str, int] = {}

    # ---- 成果（Artifact） ----
    def publish(self, key: str, value: Artifact) -> None:
        """发布成果（覆盖同名 key）。"""
        self._artifacts[key] = value
        for callback in list(self._subscribers.get(key, [])):
            try:
                callback(value)
            except Exception:
                logger.exception("黑板订阅回调失败: %s", key)
        logger.info("[blackboard] publish %s", key)

    def get(self, key: str) -> Optional[Artifact]:
        return self._artifacts.get(key)

    def get_many(self, keys: List[str]) -> Dict[str, Artifact]:
        return {k: self._artifacts[k] for k in keys if k in self._artifacts}

    def keys(self) -> List[str]:
        return list(self._artifacts.keys())

    def artifacts(self) -> Dict[str, Artifact]:
        return dict(self._artifacts)

    def subscribe(self, key: str, callback: Callable[[Artifact], None]) -> None:
        """订阅某个 key 的成果发布。"""
        self._subscribers.setdefault(key, []).append(callback)

    # ---- 主线二 2.1A：文件级写锁 ----
    @staticmethod
    def _norm_path(path: str) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def lock_file(self, path: str, holder: str) -> bool:
        """申请文件写锁；已被其他 Agent 持有时返回 False（不抢占）。"""
        key = self._norm_path(path)
        cur = self._file_locks.get(key)
        if cur is not None and cur.get("holder") != holder:
            return False
        self._file_locks[key] = {
            "holder": holder,
            "acquired_at": time.time(),
        }
        return True

    def unlock_file(self, path: str, holder: str) -> bool:
        """释放写锁；仅持有者可以释放。"""
        key = self._norm_path(path)
        cur = self._file_locks.get(key)
        if cur is None or cur.get("holder") != holder:
            return False
        del self._file_locks[key]
        return True

    def is_file_locked(self, path: str, holder: Optional[str] = None) -> bool:
        """是否被锁定；holder 非空时判断是否被该持有者锁定。"""
        cur = self._file_locks.get(self._norm_path(path))
        if cur is None:
            return False
        if holder is not None:
            return cur.get("holder") == holder
        return True

    def lock_holder(self, path: str) -> Optional[str]:
        cur = self._file_locks.get(self._norm_path(path))
        return cur.get("holder") if cur else None

    def locked_files(self) -> List[str]:
        return list(self._file_locks.keys())

    def release_all(self, holder: str) -> int:
        """释放某 Agent 持有的全部写锁（任务结束/失败兜底）。"""
        held = [k for k, v in self._file_locks.items()
                if v.get("holder") == holder]
        for k in held:
            del self._file_locks[k]
        return len(held)

    def file_locks_snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {k: dict(v) for k, v in self._file_locks.items()}

    # ---- 消息（通信协议） ----
    def post(self, message: Message) -> None:
        """把一条团队消息追加到消息日志（所有 Agent 可见）。"""
        self._messages.append(message)

    def messages(self) -> List[Message]:
        return list(self._messages)

    def find(self, msg_type: Optional[str] = None,
             sender: Optional[str] = None) -> List[Message]:
        return [
            m for m in self._messages
            if (msg_type is None or m.type == msg_type)
            and (sender is None or m.sender == sender)
        ]

    # ---- 统计 ----
    # ---- 主线二 2.3：用户超级角色消息通道 ----
    def post_user(self, message: Message) -> None:
        """用户消息入通道：强制 sender=user、置最高优先级，全员可见。"""
        if message.sender != USER_SENDER:
            message.sender = USER_SENDER
        message.priority = max(int(message.priority or 0), USER_PRIORITY)
        if not message.receiver:
            message.receiver = "*"
        self._messages.append(message)
        self._user_messages.append(message)
        logger.info("[blackboard] post_user %s -> %s", message.type,
                    message.receiver)

    def user_messages(self) -> List[Message]:
        """用户通道全量消息（回放/审计用，不消费）。"""
        return list(self._user_messages)

    def pending_user_messages(self, consumer: str) -> List[Message]:
        """返回某收听者尚未读过的用户消息（读走即消费）。

        规则：consumer == "*" 收听全部用户消息；否则仅接收投递给自己的
        消息与广播 "*"。每收听者独立游标、互不影响，未投递给自己的消息
        不阻塞后续投递。
        """
        cursor = int(self._user_cursor.get(consumer, 0))
        out: List[Message] = []
        last = cursor - 1
        for i, m in enumerate(self._user_messages[cursor:], start=cursor):
            if consumer == "*" or m.receiver in (consumer, "*"):
                out.append(m)
                last = i
        if out:
            self._user_cursor[consumer] = last + 1
        return out

    def reset_user_delivery(self, consumer: str) -> None:
        """重置某收听者的投递游标（回放/调试用）。"""
        self._user_cursor.pop(consumer, None)

    def summary(self) -> Dict[str, Any]:
        return {
            "artifacts": len(self._artifacts),
            "messages": len(self._messages),
            "user_messages": len(self._user_messages),
            "file_locks": len(self._file_locks),
            "by_type": {
                t.value: sum(1 for m in self._messages if m.type == t.value)
                for t in MsgType
            },
        }


__all__ = ["Blackboard", "Artifact"]
