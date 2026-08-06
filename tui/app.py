"""Alpha-SWE Textual TUI —— 对应设计第 14.1 节。

三栏布局：
- 左栏：思维流（thought / tool_call / 解析结果，颜色区分，可滚动）；
- 右栏：终端原始输出流（TerminalTool 逐行转发）；
- 底部状态栏：当前任务、进度、token 估算、耗时、暂停/运行状态。

交互：
- Ctrl+I：注入高优先级指令（打断当前循环）；
- Ctrl+P：暂停 / 继续；
- Ctrl+L：清空终端窗格；
- Tab：切换窗格；q / Ctrl+C：退出。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from rich.text import Text
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Label, RichLog, Static

from agent.config import AppConfig
from agent.llm import BaseLLM
from agent.mcp.manager import MCPManager
from agent.planner.planner import Planner
from agent.prompt.builder import estimate_tokens

from tui.bridge import AgentRunner
from tui.formatting import format_event
from tui.messages import (AgentEventMessage, AgentFinishedMessage,
                          AgentStartedMessage, TerminalOutputMessage)

logger = logging.getLogger("alpha-swe.tui")

CSS = """
Screen {
    layout: vertical;
    background: $surface;
}

#body {
    layout: horizontal;
    height: 1fr;
}

#thought-box {
    width: 3fr;
    border: round $primary;
    padding: 0 1;
}

#terminal-box {
    width: 2fr;
    border: round $accent;
    padding: 0 1;
}

.pane-title {
    height: 1;
    color: $text-muted;
    text-style: bold;
}

#status {
    height: 3;
    padding: 0 2;
    border-top: solid $primary;
    color: $text;
}

#status .ok { color: $success; }
#status .err { color: $error; }
#status .muted { color: $text-muted; }

#interrupt-input {
    dock: bottom;
    display: none;
    border: round $warning;
    background: $panel;
}
"""


class AlphaSWEApp(App[None]):
    """三栏式 Alpha-SWE Agent 终端界面。"""

    TITLE = "Alpha-SWE"
    SUB_TITLE = "软件工程 Agent · Textual TUI"

    # 关闭 Textual 默认 Ctrl+P 命令面板，把 Ctrl+P 留给“暂停/继续”
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+i", "interrupt", "注入指令"),
        Binding("ctrl+p", "toggle_pause", "暂停/继续"),
        Binding("ctrl+l", "clear_terminal", "清空终端"),
        Binding("tab", "focus_next", "切换窗格"),
        Binding("q", "quit", "退出"),
        Binding("ctrl+c", "quit", "退出", priority=True),
    ]

    CSS = CSS

    def __init__(
        self,
        prompt: str,
        *,
        config: Optional[AppConfig] = None,
        llm: Optional[BaseLLM] = None,
        planner: Optional[Planner] = None,
        mcp_manager: Optional[MCPManager] = None,
    ) -> None:
        super().__init__()
        self.prompt = prompt
        self._inject_config = config
        self._inject_llm = llm
        self._inject_planner = planner
        self._inject_mcp = mcp_manager
        self.runner: Optional[AgentRunner] = None
        self._started_at = time.monotonic()
        self._finished: Optional[Any] = None
        self._timer = None

    # ---- 装配 ----
    def _make_runner(self) -> AgentRunner:
        return AgentRunner(
            self,
            self.prompt,
            config=self._inject_config,
            llm=self._inject_llm,
            planner=self._inject_planner,
            mcp_manager=self._inject_mcp,
        )

    # ---- 生命周期 ----
    def compose(self):
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="thought-box"):
                yield Label("🧠 思维流", classes="pane-title")
                yield RichLog(id="thought-log", highlight=True, markup=True,
                              wrap=True, auto_scroll=True)
            with Vertical(id="terminal-box"):
                yield Label("🖥 终端输出", classes="pane-title")
                yield RichLog(id="terminal-log", highlight=True, markup=True,
                              wrap=True, auto_scroll=True)
        yield Static(id="status")
        yield Input(placeholder="输入高优先级指令，Enter 注入（Esc 取消）…",
                    id="interrupt-input")
        yield Footer()

    def on_mount(self) -> None:
        self.runner = self._make_runner()
        self.run_agent()
        self._timer = self.set_interval(0.5, self.refresh_status)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    # ---- Worker：跑 AgentLoop ----
    @work(thread=False, exclusive=False)
    async def run_agent(self) -> None:
        assert self.runner is not None
        try:
            await self.runner.run()
        except Exception as e:  # 运行异常也要让 UI 得知
            logger.exception("Agent 运行异常")
            self.post_message(AgentFinishedMessage(None))
            self._finished = e

    # ---- 消息处理 ----
    def on_agent_started_message(self, msg: AgentStartedMessage) -> None:
        self._started_at = time.monotonic()
        self._append_thought(Text(f"▶ 会话开始：{msg.prompt}", style="bold white"))
        self.refresh_status()

    def on_agent_event_message(self, msg: AgentEventMessage) -> None:
        record = msg.record
        self._append_thought(format_event(record))
        # 工具调用时在右栏显示分隔与命令摘要
        if record.get("type") == "tool_call":
            self._append_terminal(
                Text(f"── {record['data'].get('tool', '')} ─────────────",
                     style="dim"))
        self.refresh_status()

    def on_terminal_output_message(self, msg: TerminalOutputMessage) -> None:
        self._append_terminal(msg.text)

    def on_agent_finished_message(self, msg: AgentFinishedMessage) -> None:
        self._finished = msg.result
        self.refresh_status()

    # ---- 视图 ----
    def _append_thought(self, renderable) -> None:
        self.query_one("#thought-log", RichLog).write(renderable)

    def _append_terminal(self, renderable) -> None:
        self.query_one("#terminal-log", RichLog).write(renderable)

    def refresh_status(self) -> None:
        """按当前 runner/loop 状态刷新底部状态栏。"""
        status = self.query_one("#status", Static)
        runner = self.runner
        if runner is None:
            return
        elapsed = time.monotonic() - self._started_at
        parts: list[Text] = []
        parts.append(Text(f"⏱ {elapsed:.1f}s ", style="bold"))

        if self._finished is not None:
            if isinstance(self._finished, Exception):
                parts.append(Text(f"✖ 运行异常: {self._finished}", style="bold red"))
            else:
                ok = bool(self._finished and self._finished.ok)
                style = "bold green" if ok else "bold red"
                text = (f"🏁 完成 ({self._finished.phase.value})"
                        if self._finished else "🏁 结束")
                parts.append(Text(text, style=style))
        else:
            loop = runner.loop
            if loop is not None and loop.paused:
                parts.append(Text("⏸ 已暂停", style="bold yellow"))
            else:
                parts.append(Text("▶ 运行中", style="bold cyan"))

        task = runner.running_task()
        if task is not None:
            parts.append(Text(f" | 当前任务 {task.id}: {task.instruction[:40]}",
                              style=""))
        summary = runner.dag_summary()
        if summary:
            bits = " ".join(f"{k}:{v}" for k, v in sorted(summary.get("by_status", {}).items()))
            parts.append(Text(f" | 任务 {bits}", style="dim"))
        rounds = runner.total_rounds()
        parts.append(Text(f" | 轮次 {rounds}", style="dim"))
        tokens = self._estimate_tokens()
        parts.append(Text(f" | ~{tokens} tokens", style="dim"))
        status.update(Text("").join(parts))

    def _estimate_tokens(self) -> int:
        runner = self.runner
        if runner is None or runner.loop is None:
            return 0
        total = 0
        for t in runner.loop.scheduler.dag.all():
            for h in t.history:
                total += estimate_tokens(str(h.get("content", "")))
        return total

    # ---- 动作 ----
    def action_interrupt(self) -> None:
        """Ctrl+I：显示/聚焦指令输入框。"""
        box = self.query_one("#interrupt-input", Input)
        if box.display:
            box.display = False
            self.set_focus(self.query_one("#thought-log"))
            return
        box.display = True
        box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        box = self.query_one("#interrupt-input", Input)
        box.display = False
        box.value = ""
        if not value:
            return
        runner = self.runner
        if runner is None or runner.loop is None:
            self._append_thought(Text("⚠ Agent 尚未就绪，无法注入指令", style="yellow"))
            return
        runner.loop.interrupt(value)
        self._append_thought(format_event({"type": "interrupt", "data": {"prompt": value}}))

    def action_toggle_pause(self) -> None:
        runner = self.runner
        if runner is None or runner.loop is None:
            return
        if runner.loop.paused:
            runner.loop.resume()
            self._append_thought(Text("▶ 已恢复运行", style="green"))
        else:
            runner.loop.pause()
            self._append_thought(Text("⏸ 已暂停（Ctrl+P 恢复）", style="yellow"))
        self.refresh_status()

    def action_clear_terminal(self) -> None:
        self.query_one("#terminal-log", RichLog).clear()


__all__ = ["AlphaSWEApp"]
