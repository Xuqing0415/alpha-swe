"""TUI 桥接 —— 在 Textual App 的事件循环内运行 AgentLoop。

- AgentLoop.subscribe() 把事件同步回调进来，转成 Textual Message；
- TerminalTool 的 output_callback 把命令输出逐行转发到右栏；
- 结束/异常时发送 AgentFinishedMessage。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agent.config import AppConfig
from agent.core.loop import AgentLoop, LoopResult
from agent.core.task import Task
from agent.llm import BaseLLM
from agent.mcp.manager import MCPManager
from agent.planner.planner import Planner

from tui.messages import (AgentEventMessage, AgentFinishedMessage,
                          AgentStartedMessage, TerminalOutputMessage)

logger = logging.getLogger("alpha-swe.tui.bridge")


class AgentRunner:
    """持有 AgentLoop 并向 App 转发事件的运行器。"""

    def __init__(
        self,
        app: Any,
        prompt: str,
        *,
        config: Optional[AppConfig] = None,
        llm: Optional[BaseLLM] = None,
        planner: Optional[Planner] = None,
        mcp_manager: Optional[MCPManager] = None,
    ) -> None:
        self.app = app
        self.prompt = prompt
        self.config = config
        self.llm = llm
        self.planner = planner
        self.mcp_manager = mcp_manager
        self.loop: Optional[AgentLoop] = None
        self.result: Optional[LoopResult] = None

    def build_loop(self) -> AgentLoop:
        """构造 AgentLoop（连接输出回调与事件订阅）。"""
        loop = AgentLoop(
            config=self.config,
            llm=self.llm,
            planner=self.planner,
            mcp_manager=self.mcp_manager,
            output_callback=self._on_terminal_output,
        )
        loop.subscribe(self._on_event)
        return loop

    async def run(self) -> LoopResult:
        self.app.post_message(AgentStartedMessage(self.prompt))
        self.loop = self.build_loop()
        try:
            self.result = await self.loop.run(self.prompt)
            return self.result
        finally:
            await self.loop.close()
            self.app.post_message(AgentFinishedMessage(self.result))

    # ---- 回调（事件循环内同步调用） ----
    def _on_event(self, record: Dict[str, Any]) -> None:
        self.app.post_message(AgentEventMessage(record))

    def _on_terminal_output(self, line: str) -> None:
        self.app.post_message(TerminalOutputMessage(line))

    # ---- 状态查询（供状态栏刷新） ----
    def running_task(self) -> Optional[Task]:
        if self.loop is None:
            return None
        for t in self.loop.scheduler.dag.all():
            if t.status.value == "running":
                return t
        return None

    def dag_summary(self) -> Dict[str, int]:
        if self.loop is None:
            return {}
        return self.loop.scheduler.dag.summary()

    def total_rounds(self) -> int:
        if self.loop is None:
            return 0
        return sum(t.round_count for t in self.loop.scheduler.dag.all())


__all__ = ["AgentRunner"]
