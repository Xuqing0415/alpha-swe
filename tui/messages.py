"""TUI 消息 —— 从 AgentLoop/终端到 Textual 界面的事件消息。

所有消息都通过 Textual 的 Message 传递，保证在事件循环内被 UI 安全消费。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

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


class LogMessage(Message):
    """Python logging 记录转发到主日志区（隔离 stdout，避免屏幕乱码）。"""

    def __init__(self, level: str, content: str) -> None:
        self.level = level
        self.content = content
        super().__init__()


class ConfirmationRequestMessage(Message):
    """请求用户确认高风险工具调用（阶段八 8.2）。"""

    def __init__(self, tool_name: str, params: Dict[str, Any],
                 rule: Optional[str] = None) -> None:
        self.tool_name = tool_name
        self.params = params
        self.rule = rule
        super().__init__()


__all__ = [
    "AgentEventMessage",
    "TerminalOutputMessage",
    "AgentFinishedMessage",
    "AgentStartedMessage",
    "LogMessage",
    "ConfirmationRequestMessage",
]
