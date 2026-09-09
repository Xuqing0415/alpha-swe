"""轻量会话状态事件总线（主线一 1.3C）。

让 TUI / 观测面板与 AgentLoop 解耦：TUI 不直接读取 ``SessionState``
内部实现，而是在状态变更点订阅通知。

用法::

    from agent.core import events

    def on_update(state):
        # state 是 SessionState 实例；如需跨进程只读快照，用 state.to_dict()
        ...

    events.subscribe(on_update)
    events.notify_tui(state)
    events.unsubscribe(on_update)

AgentLoop 在每个会话变更点调用 ``notify_tui``，同时把快照以
``session_state`` 事件类型发给自身订阅者（CLI JSON / Textual 主日志）。
注册表是进程内的全局轻量总线；同一进程并存多个 AgentLoop 时，所有订阅方
都会收到每个实例的变更，订阅方应按 ``state.session_id`` 过滤。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List

logger = logging.getLogger("alpha-swe.session_events")

_subscribers: List[Callable[[Any], None]] = []


def subscribe(callback: Callable[[Any], None]) -> Callable[[Any], None]:
    """注册会话状态变更回调。返回回调本身，便于 ``unsubscribe`` 复用。"""
    if callback not in _subscribers:
        _subscribers.append(callback)
    return callback


def unsubscribe(callback: Callable[[Any], None]) -> None:
    """注销回调（不存在时静默）。"""
    try:
        _subscribers.remove(callback)
    except ValueError:
        pass


def clear() -> None:
    """清空全部订阅（主要用于测试隔离）。"""
    _subscribers.clear()


def notify_tui(state: Any) -> None:
    """广播一次会话状态变更；回调异常只记日志，不影响主流程。"""
    for callback in list(_subscribers):
        try:
            callback(state)
        except Exception:  # noqa: BLE001
            logger.exception("会话状态回调执行失败")


__all__ = ["clear", "notify_tui", "subscribe", "unsubscribe"]
