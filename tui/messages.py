"""TUI 消息 —— 从 AgentLoop/终端到 Textual 界面的事件消息。

所有消息都通过 Textual 的 Message 传递，保证在事件循环内被 UI 安全消费。
"""
from __future__ import annotations

from typing import Any, Dict

from textual.message import Message


class AgentEventMessage(Message):
    """一条 Agent 事件（think / tool_call / task_done / ...）。"""

    def __init__(self, record: Dict[str, Any]) -> None:
        self.record = record
        super().__init__()


class TerminalOutputMessage(Message):
    """终端工具输出的一行原始文本（右栏实时流）。"""

    def __init__(self, text: str, tool: str = "terminal") -> None:
        self.text = text
        self.tool = tool
        super().__init__()


class AgentFinishedMessage(Message):
    """AgentLoop.run() 结束（无论成功/失败）。"""

    def __init__(self, result: Any) -> None:
        self.result = result
        super().__init__()


class AgentStartedMessage(Message):
    """AgentLoop.run() 已启动。"""

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        super().__init__()


__all__ = [
    "AgentEventMessage",
    "TerminalOutputMessage",
    "AgentFinishedMessage",
    "AgentStartedMessage",
]
