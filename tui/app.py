"""Alpha-SWE Textual TUI —— 对应设计第 14.1 节 + 阶段八交互升级。

布局：
- 左栏：TabbedContent 多视图 —— 思维流 / 任务树 / 监控 / 文件变更；
- 右栏：终端原始输出流（TerminalTool 逐行转发）；
- 底部状态栏：当前任务、进度、token 估算、耗时、暂停/运行状态。

交互：
- f：轮换左栏视图（思维流 -> 任务树 -> 监控 -> 文件变更）；
- Ctrl+I：注入高优先级指令（打断当前循环）；
- Ctrl+P：暂停 / 继续；
- Ctrl+L：清空终端窗格；
- Tab：切换窗格；q / Ctrl+C：退出。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, List, Optional

from rich.text import Text
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Footer, Header, Input, Label, RichLog, Static,
                             TabPane, TabbedContent)

from agent.config import AppConfig
from agent.llm import BaseLLM
from agent.mcp.manager import MCPManager
from agent.planner.planner import Planner
from agent.prompt.builder import estimate_tokens

from tui.bridge import AgentRunner
from tui.formatting import format_event
from tui.messages import (AgentEventMessage, AgentFinishedMessage,
                          AgentStartedMessage, ConfirmationRequestMessage,
                          TerminalOutputMessage)

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

#left-box {
    width: 3fr;
    border: round $primary;
    padding: 0 1;
}

#left-box TabbedContent {
    height: 1fr;
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

#thought-log {
    height: 1fr;
}

#task-tree-view, #metrics-view {
    height: 1fr;
}

#diff-log {
    height: 1fr;
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

/* 高风险操作确认弹窗（阶段八 8.2） */
ConfirmationScreen {
    align: center middle;
}

#confirm-box {
    width: 76;
    height: auto;
    border: round $error;
    padding: 1 2;
    background: $panel;
}

#confirm-body {
    height: auto;
    margin-bottom: 1;
}

#confirm-input {
    height: 3;
}
"""

_VIEW_ORDER = ["tab-thought", "tab-tasktree", "tab-metrics", "tab-diff"]

_TASK_ICON = {
    "idle": "○", "planning": "◌", "ready": "⭘", "running": "▶",
    "waiting": "⏳", "completed": "✔", "failed": "✖",
}


class ConfirmationScreen(ModalScreen[Any]):
    """高风险工具调用确认弹窗：批准一次 / 批准所有同类 / 拒绝 / 修改参数。"""

    BINDINGS = [
        Binding("escape", "reject", "拒绝"),
    ]

    def __init__(self, tool_name: str, params: dict, rule: Optional[str]) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.params = params
        self.rule = rule or ""

    def compose(self):
        with Vertical(id="confirm-box"):
            yield Label("⚠ 需要确认", classes="pane-title")
            yield Static(
                f"[bold]工具[/bold]: {self.tool_name}\n"
                f"[bold]规则[/bold]: {self.rule or '（无）'}\n"
                f"[bold]参数[/bold]: {_short(self.params, 300)}",
                id="confirm-body",
            )
            yield Input(
                placeholder=("y 批准一次 | a 批准所有同类 | n 拒绝 | "
                             "m:{\"path\":\"...\"} 修改参数后执行"),
                id="confirm-input",
            )

    def on_mount(self) -> None:
        self.query_one("#confirm-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = (event.value or "").strip().lower()
        box = self.query_one("#confirm-input", Input)
        box.value = ""
        if value in ("y", "yes", "approve", "1"):
            self.dismiss(True)
        elif value in ("a", "all", "approve_all", "2"):
            self.dismiss("approved_all:" + (self.rule or self.tool_name))
        elif value in ("n", "no", "reject", "0"):
            self.dismiss(False)
        elif value.startswith("m:"):
            try:
                modified = json.loads(value[2:])
                self.dismiss(modified if isinstance(modified, dict) else True)
            except json.JSONDecodeError:
                self.dismiss(True)  # 参数解析失败时按批准一次处理
        else:
            self.query_one("#confirm-input", Input).value = ""

    def action_reject(self) -> None:
        self.dismiss(False)


class AlphaSWEApp(App[None]):
    """多视图 Alpha-SWE Agent 终端界面。"""

    TITLE = "Alpha-SWE"
    SUB_TITLE = "软件工程 Agent · Textual TUI"

    # 关闭 Textual 默认 Ctrl+P 命令面板，把 Ctrl+P 留给“暂停/继续”
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("f", "cycle_view", "切换视图"),
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
        self._confirmation_queue: List[ConfirmationRequestMessage] = []
        self._confirmation_open = False
        self._confirmations_asked = 0  # 测试用：已发出确认请求数

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
            with Vertical(id="left-box"):
                with TabbedContent(initial="tab-thought"):
                    with TabPane("🧠 思维流", id="tab-thought"):
                        yield RichLog(id="thought-log", highlight=True,
                                      markup=True, wrap=True, auto_scroll=True)
                    with TabPane("🗂 任务树", id="tab-tasktree"):
                        yield Static(id="task-tree-view", markup=True)
                    with TabPane("📊 监控", id="tab-metrics"):
                        yield Static(id="metrics-view", markup=True)
                    with TabPane("🖊 文件变更", id="tab-diff"):
                        yield RichLog(id="diff-log", highlight=True,
                                      markup=True, wrap=True, auto_scroll=True)
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
        except Exception as e:  # 兜底：worker 异常也要结束会话
            self.runner.result = e
            self._finished = e
            self.refresh_status()
            raise

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
            self._append_diff_event(record["data"])
        self.refresh_status()

    def on_terminal_output_message(self, msg: TerminalOutputMessage) -> None:
        self._append_terminal(msg.text)

    def on_agent_finished_message(self, msg: AgentFinishedMessage) -> None:
        self._finished = msg.result
        self.refresh_status()

    # ---- 确认弹窗（阶段八 8.2） ----
    def on_confirmation_request_message(
            self, msg: ConfirmationRequestMessage) -> None:
        self._confirmation_queue.append(msg)
        self._maybe_show_confirmation()

    def _maybe_show_confirmation(self) -> None:
        if self._confirmation_open or not self._confirmation_queue:
            return
        msg = self._confirmation_queue.pop(0)
        self._confirmation_open = True
        self._confirmations_asked += 1
        self.push_screen(
            ConfirmationScreen(msg.tool_name, msg.params, msg.rule),
            callback=self._on_confirmation_result,
        )

    def _on_confirmation_result(self, decision: Any) -> None:
        self._confirmation_open = False
        if self.runner is not None:
            self.runner.resolve_confirmation(decision)
        self._maybe_show_confirmation()

    # ---- 视图 ----
    def _append_thought(self, renderable) -> None:
        self.query_one("#thought-log", RichLog).write(renderable)

    def _append_terminal(self, renderable) -> None:
        self.query_one("#terminal-log", RichLog).write(renderable)

    def _append_diff_event(self, data: dict) -> None:
        """把文件写操作渲染成类 diff 行，进入文件变更视图。"""
        tool = data.get("tool", "")
        params = data.get("params", {}) or {}
        action = params.get("action", "")
        if tool == "file_ops" and action in ("write", "append"):
            self._append_diff(Text(
                f"✏ {params.get('path', '')} ({action})",
                style="bold yellow"))
            content = str(params.get("content", ""))
            for line in content.splitlines()[:60]:
                self._append_diff(Text(f"  + {line}", style="green"))
        else:
            self._append_diff(Text(
                f"⚙ {tool} {_short(params, 120)}", style="dim"))

    def _append_diff(self, renderable) -> None:
        self.query_one("#diff-log", RichLog).write(renderable)

    def refresh_status(self) -> None:
        """按当前 runner/loop 状态刷新底部状态栏与监控/任务树视图。"""
        status = self.query_one("#status", Static)
        runner = self.runner
        if runner is None:
            return
        elapsed = time.monotonic() - self._started_at
        parts: list[Text] = []
        parts.append(Text(f"⏱ {elapsed:.1f}s ", style="bold"))

        if self._finished is not None:
            if isinstance(self._finished, Exception):
                parts.append(Text(f"✖ 运行异常: {self._finished}",
                                  style="bold red"))
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
            bits = " ".join(f"{k}:{v}" for k, v in
                            sorted(summary.get("by_status", {}).items()))
            parts.append(Text(f" | 任务 {bits}", style="dim"))
        rounds = runner.total_rounds()
        parts.append(Text(f" | 轮次 {rounds}", style="dim"))
        tokens = self._estimate_tokens()
        parts.append(Text(f" | ~{tokens} tokens", style="dim"))
        status.update(Text("").join(parts))

        self.refresh_views()

    def refresh_views(self) -> None:
        """刷新任务树与监控视图（阶段八 8.1）。"""
        runner = self.runner
        if runner is None or runner.loop is None:
            return
        # 任务树视图
        tree = self.query_one("#task-tree-view", Static)
        rows = []
        for t in runner.loop.scheduler.dag.all():
            icon = _TASK_ICON.get(t.status.value, "·")
            deps = f" ← {','.join(t.dependencies)}" if t.dependencies else ""
            rows.append(
                f"{icon} {t.id} [{t.status.value}] "
                f"{t.instruction[:48]}{deps}"
            )
        tree.update("\n".join(rows) if rows else "（暂无任务）")
        # 监控视图
        mon = self.query_one("#metrics-view", Static)
        snap = runner.metrics_snapshot()
        alerts = runner.metrics_alerts()
        d = snap.get("derived", {})
        lines = [
            f"阶段: {d.get('phase', '-')}    轮次: {d.get('rounds', 0)}",
            f"token: {d.get('token_total', 0)}  "
            f"（速率 {d.get('token_rate', 0)}/s）",
            f"工具: {d.get('tool_calls', 0)} 次，"
            f"成功率 {d.get('tool_success_rate', '-')}",
            f"LLM 调用: {d.get('llm_calls', 0)}    "
            f"连续失败: {d.get('consecutive_failures', 0)}",
            f"压缩: {d.get('compressions', 0)}    "
            f"重试: {d.get('retries', 0)}    "
            f"中断: {d.get('interrupts', 0)}",
        ]
        if alerts:
            lines.append("")
            lines.extend(f"⚠ {a}" for a in alerts)
        mon.update("\n".join(lines))

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
    def action_cycle_view(self) -> None:
        """f 键轮换左栏视图。"""
        tabbed = self.query_one(TabbedContent)
        current = str(tabbed.active or "")
        try:
            idx = _VIEW_ORDER.index(current)
        except ValueError:
            idx = -1
        tabbed.active = _VIEW_ORDER[(idx + 1) % len(_VIEW_ORDER)]

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
        # 只处理主界面的中断指令输入；确认弹窗的输入由 ConfirmationScreen 消费
        if getattr(event.input, "id", None) != "interrupt-input":
            return
        value = event.value.strip()
        box = self.query_one("#interrupt-input", Input)
        box.display = False
        box.value = ""
        if not value:
            return
        runner = self.runner
        if runner is None or runner.loop is None:
            self._append_thought(Text("⚠ Agent 尚未就绪，无法注入指令",
                                      style="yellow"))
            return
        runner.loop.interrupt(value)
        self._append_thought(format_event(
            {"type": "interrupt", "data": {"prompt": value}}))

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


def _short(value: Any, limit: int) -> str:
    text = str(value).replace("\n", "⏎ ")
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = ["AlphaSWEApp", "ConfirmationScreen"]
