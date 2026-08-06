"""黑板（Shared Blackboard）—— 对应设计第 8 节。

内存共享状态：Worker 发布成果（diff / 测试报告 / 文件清单），Orchestrator 汇总。
接口与 Redis Pub/Sub 对齐：publish / get / subscribe，便于将来替换实现。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from agent.multiagent.messages import Message, MsgType

logger = logging.getLogger("alpha-swe.multiagent.blackboard")

Artifact = Dict[str, Any]


class Blackboard:
    """线程/协程安全的内存黑板（单事件循环内同步访问）。"""

    def __init__(self) -> None:
        self._artifacts: Dict[str, Artifact] = {}
        self._messages: List[Message] = []
        self._subscribers: Dict[str, List[Callable[[Artifact], None]]] = {}

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
    def summary(self) -> Dict[str, Any]:
        return {
            "artifacts": len(self._artifacts),
            "messages": len(self._messages),
            "by_type": {
                t.value: sum(1 for m in self._messages if m.type == t.value)
                for t in MsgType
            },
        }


__all__ = ["Blackboard", "Artifact"]
