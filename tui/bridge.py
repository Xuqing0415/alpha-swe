"""TUI 桥接 —— 在 Textual App 的事件循环内运行 AgentLoop。

- AgentLoop.subscribe() 把事件同步回调进来，转成 Textual Message；
- TerminalTool 的 output_callback 把命令输出逐行转发到右栏；
- 结束/异常时发送 AgentFinishedMessage。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from agent.config import AppConfig
from agent.core.loop import AgentLoop, LoopResult
from agent.core.task import Task
from agent.llm import BaseLLM
from agent.mcp.manager import MCPManager
from agent.planner.planner import Planner

from tui.messages import (AgentEventMessage, AgentFinishedMessage,
                          AgentStartedMessage, ConfirmationRequestMessage,
                          TerminalOutputMessage)

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
        self.approve_rule: Optional[str] = None
        # 等待 TUI 用户决定的确认请求队列（支持并行工具调用的多次确认）
        self._confirmation_futures: List[Any] = []

    def build_loop(self) -> AgentLoop:
        """构造 AgentLoop（连接输出回调与事件订阅）。"""
        loop = AgentLoop(
            config=self.config,
            llm=self.llm,
            planner=self.planner,
            mcp_manager=self.mcp_manager,
            output_callback=self._on_terminal_output,
            confirmation_callback=self._on_confirmation,
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

    # ---- 高风险操作确认（阶段八 8.2） ----
    async def _on_confirmation(self, tool_name: str, params: Dict[str, Any],
                                rule: Optional[str] = None) -> Any:
        """请求 TUI 弹窗确认；挂起直到用户选择，返回决定。

        决定取值：True（批准一次）/ False（拒绝）/
        "approved_all:<rule>"（批准所有同类）/ dict（批准并修改参数）。
        """
        if self.approve_rule and self.approve_rule == (rule or tool_name):
            return "approved_all:" + (rule or tool_name)
        future: Any = asyncio.get_running_loop().create_future()
        self._confirmation_futures.append(future)
        self.app.post_message(
            ConfirmationRequestMessage(tool_name, params, rule)
        )
        try:
            return await future
        except asyncio.CancelledError:
            try:
                self._confirmation_futures.remove(future)
            except ValueError:
                pass
            return False

    def resolve_confirmation(self, decision: Any) -> None:
        """App 侧把用户选择写回最早等待的确认请求。"""
        if not self._confirmation_futures:
            return
        future = self._confirmation_futures.pop(0)
        if isinstance(decision, str) and decision.startswith("approved_all:"):
            self.approve_rule = decision[len("approved_all:"):]
        if not future.done():
            future.set_result(decision)

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

    def metrics_snapshot(self) -> Dict[str, Any]:
        """实时指标快照（监控视图渲染用）。"""
        if self.loop is None:
            return {}
        return self.loop.metrics.snapshot()

    def metrics_alerts(self) -> List[str]:
        """当前告警列表（token 速率 / 连续失败 / 轮次逼近上限）。"""
        if self.loop is None:
            return []
        return self.loop.metrics.alerts(max_rounds=self.loop._max_rounds)


__all__ = ["AgentRunner"]
