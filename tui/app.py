"""Alpha-SWE Textual TUI —— 纯终端 UI 设计（无 emoji，信息密度优先）。

布局（宽屏 >=100 列）：
- 左栏任务面板（30 列）：任务名 / 阶段 / 任务树 / 进度条 / 耗时；
- 中栏主日志区：`[HH:MM:SS] TYPE 内容` 实时滚动；
- 右下终端输出区（6 行，F3 全屏）；
- 底部状态栏（右对齐：tokens / round / mem / session）与输入栏。

窄屏（<100 列）自动降级为单栏：顶部紧凑头 + 主日志 + 终端 + 状态栏。

交互：
- F1 帮助 / F2 任务面板 / F3 终端全屏 / F4 宽窄切换 / F5 主区视图；
- Ctrl+I 注入指令 / Ctrl+P 暂停 / Ctrl+R 重试 / Ctrl+S 跳过；
- 输入栏支持 /pause /resume /status /retry /skip /quit，上下箭头浏览历史。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, List, Optional

from rich.text import Text
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, RichLog, Static

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

_NARROW_WIDTH = 100
_MAIN_VIEWS = ["log", "diff", "metrics"]
_PHASE_COLORS = {
    "idle": "bright_black", "planning": "cyan", "ready": "cyan",
    "running": "yellow", "waiting": "yellow",
    "completed": "green", "failed": "red",
}
_TASK_MARKS = {
    "completed": ("完成 ", "green"),
    "running": ("进行>", "yellow"),
    "waiting": ("等待 ", "bright_black"),
    "ready": ("就绪 ", "bright_black"),
    "failed": ("失败 ", "red"),
    "idle": ("空闲 ", "bright_black"),
    "planning": ("规划 ", "cyan"),
}

CSS = """
Screen {
    layout: vertical;
    background: $surface;
}

#main {
    height: 1fr;
    layout: vertical;
}

#compact-header {
    height: 1;
    display: none;
    padding: 0 1;
    color: $text-muted;
    border-bottom: solid $primary;
}

#body {
    height: 1fr;
    layout: horizontal;
}

#task-panel {
    width: 30;
    border: round white;
    padding: 0 1;
    margin: 0 1 0 0;
}

#main-area {
    width: 1fr;
    layout: vertical;
}

#main-log, #diff-log, #metrics-view {
    height: 1fr;
}

#diff-log, #metrics-view {
    display: none;
}

#terminal-box {
    height: 6;
    border-top: solid white;
    padding: 0 1;
}

#terminal-title {
    height: 1;
    color: $text-muted;
    text-style: bold;
}

#terminal-log {
    height: 1fr;
}

#status-bar {
    height: 1;
    padding: 0 1;
    border-top: solid white;
    color: $text;
    text-align: right;
}

#input-row {
    height: 1;
    padding: 0 1;
    border-top: solid white;
}

#input-prompt {
    width: 2;
    content-align: center middle;
    color: $text-muted;
}

#input-bar {
    height: 1;
}

/* 弹窗 */
HelpScreen, TerminalScreen {
    align: center middle;
}

#help-box {
    width: 76;
    height: auto;
    border: round white;
    padding: 1 2;
    background: $panel;
}

#term-box {
    width: 90%;
    height: 90%;
    border: round white;
    padding: 0 1;
    background: $panel;
}

#term-full-log {
    height: 1fr;
}

ConfirmationScreen {
    align: center middle;
}

#confirm-box {
    width: 76;
    height: auto;
    border: round white;
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

_HELP_TEXT = """[bold]Alpha-SWE 快捷键[/bold]

[green]F1[/green]  帮助      [green]F2[/green]  任务面板显示/隐藏
[green]F3[/green]  终端全屏  [green]F4[/green]  宽屏/窄屏切换
[green]F5[/green]  主区视图（日志/文件变更/监控）
[green]Ctrl+I[/green]  注入指令    [green]Ctrl+P[/green]  暂停/继续
[green]Ctrl+R[/green]  重试当前步骤 [green]Ctrl+S[/green]  跳过当前步骤
[green]Ctrl+L[/green]  清空终端    [green]Tab[/green]    切换焦点
[green]上/下[/green]  输入历史    [green]Esc[/green]    清空输入
[green]Ctrl+C[/green] / [green]q[/green]  退出

[bold]输入命令（/ 开头）[/bold]
/pause 暂停   /resume 恢复   /status 详细状态
/retry 重试   /skip 跳过     /quit 退出

[bold]日志类型[/bold]
[cyan]THINK[/cyan] 思考   [white]ACT[/white] 动作   OBS 观察
INFO 信息   [yellow]WARN[/yellow] 警告   [red]ERROR[/red] 错误
[green]OK[/green] 成功   MEM 记忆
"""


class CommandInput(Input):
    """带输入历史 / 清空键的输入栏（设计：键盘优先）。"""

    BINDINGS = [
        Binding("up", "history_prev", "上一条", show=False),
        Binding("down", "history_next", "下一条", show=False),
        Binding("escape", "clear_value", "清空", show=False),
    ]

    def __init__(self, host: "AlphaSWEApp", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._host = host

    def action_history_prev(self) -> None:
        self._host.input_history_prev()

    def action_history_next(self) -> None:
        self._host.input_history_next()

    def action_clear_value(self) -> None:
        self.value = ""


class ConfirmationScreen(ModalScreen[Any]):
    """高风险工具调用确认弹窗：批准一次 / 批准所有同类 / 拒绝 / 编辑参数。"""

    BINDINGS = [
        Binding("escape", "reject", "拒绝"),
    ]

    def __init__(self, tool_name: str, params: dict,
                 rule: Optional[str]) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.params = params
        self.rule = rule or ""

    def compose(self):
        with Vertical(id="confirm-box"):
            yield Label("Agent 请求执行", classes="pane-title")
            yield Static(
                f"[bold]工具[/bold]: {self.tool_name}\n"
                f"[bold]规则[/bold]: {self.rule or '（无）'}\n"
                f"[bold]参数[/bold]: {_short(self.params, 300)}",
                id="confirm-body",
            )
            yield Input(
                placeholder=("y 批准 | a 批准所有同类 | n 拒绝 | "
                             "e:{\"path\":\"...\"} 编辑参数"),
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
        elif value.startswith("e:") or value.startswith("m:"):
            try:
                modified = json.loads(value[2:])
                self.dismiss(modified if isinstance(modified, dict) else True)
            except json.JSONDecodeError:
                self.dismiss(True)  # 参数解析失败时按批准一次处理
        else:
            box.value = ""

    def action_reject(self) -> None:
        self.dismiss(False)


class HelpScreen(ModalScreen[None]):
    """F1 帮助：快捷键与命令列表。"""

    BINDINGS = [Binding("escape", "close_help", "关闭")]

    def compose(self):
        with Vertical(id="help-box"):
            yield Static(_HELP_TEXT, markup=True)

    def action_close_help(self) -> None:
        self.dismiss(None)


class TerminalScreen(ModalScreen[None]):
    """F3 终端全屏视图：显示最近终端输出，Esc 退出。"""

    BINDINGS = [Binding("escape", "close_term", "关闭")]

    def __init__(self, lines: List[Text]) -> None:
        super().__init__()
        self._lines = lines

    def compose(self):
        with Vertical(id="term-box"):
            yield Label("终端输出（Esc 退出）", classes="pane-title")
            yield RichLog(id="term-full-log", wrap=True, auto_scroll=True)

    def on_mount(self) -> None:
        log = self.query_one("#term-full-log", RichLog)
        log.clear()
        for line in self._lines[-500:]:
            log.write(line)

    def action_close_term(self) -> None:
        self.dismiss(None)


class AlphaSWEApp(App[None]):
    """纯终端风格的 Alpha-SWE Agent 界面（设计：信息优先、键盘优先）。"""

    TITLE = "Alpha-SWE"
    SUB_TITLE = "软件工程 Agent"

    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("f1", "show_help", "帮助"),
        Binding("f2", "toggle_task_panel", "任务面板"),
        Binding("f3", "show_terminal", "终端全屏"),
        Binding("f4", "toggle_layout", "宽/窄"),
        Binding("f5", "cycle_main_view", "主区视图"),
        Binding("ctrl+i", "focus_input", "注入指令"),
        Binding("ctrl+p", "toggle_pause", "暂停/继续"),
        Binding("ctrl+r", "retry_step", "重试"),
        Binding("ctrl+s", "skip_step", "跳过"),
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
        self.config = config
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
        self._input_history: List[str] = []
        self._hist_idx: Optional[int] = None
        self._terminal_lines: List[Text] = []
        self._main_view = "log"
        self._layout_override: Optional[str] = None
        self._task_panel_visible = True
        self._session_id = uuid.uuid4().hex[:6]

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
        with Vertical(id="main"):
            yield Static("", id="compact-header", markup=True)
            with Horizontal(id="body"):
                yield Static("", id="task-panel", markup=True)
                with Vertical(id="main-area"):
                    yield RichLog(id="main-log", highlight=True, markup=True,
                                  wrap=True, auto_scroll=True)
                    yield RichLog(id="diff-log", highlight=True, markup=True,
                                  wrap=True, auto_scroll=True)
                    yield Static("", id="metrics-view", markup=True)
                    with Vertical(id="terminal-box"):
                        yield Label("终端输出", id="terminal-title")
                        yield RichLog(id="terminal-log", highlight=True,
                                      markup=True, wrap=True,
                                      auto_scroll=True, max_lines=1000)
        yield Static("", id="status-bar", markup=True)
        with Horizontal(id="input-row"):
            yield Static(">", id="input-prompt")
            yield CommandInput(
                self, id="input-bar",
                placeholder="输入指令，/ 开头为命令（/status /pause /skip ...）")
        yield Footer()

    def on_mount(self) -> None:
        self.runner = self._make_runner()
        self.run_agent()
        self._timer = self.set_interval(0.5, self.refresh_status)
        self._apply_layout()

    def on_resize(self, event: Any) -> None:
        if not self.is_mounted:
            return
        self._apply_layout()

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
        self._append_thought(format_event(
            {"type": "run_start", "data": {"prompt": msg.prompt}}))
        self.refresh_status()

    def on_agent_event_message(self, msg: AgentEventMessage) -> None:
        record = msg.record
        self._append_thought(format_event(record))
        # 工具调用时在终端区显示分隔与命令摘要，同时进入文件变更视图
        if record.get("type") == "tool_call":
            self._append_terminal(
                Text(f"--- {record['data'].get('tool', '')} ---------------",
                     style="dim"))
            self._append_diff_event(record["data"])
        self.refresh_status()

    def on_terminal_output_message(self, msg: TerminalOutputMessage) -> None:
        self._append_terminal(Text(msg.text, style=""))

    def on_agent_finished_message(self, msg: AgentFinishedMessage) -> None:
        self._finished = msg.result
        self.refresh_status()

    def on_log_message(self, msg: LogMessage) -> None:
        """Python logging 记录渲染进主日志区（INFO/WARN/ERROR）。"""
        level = msg.level.upper()
        tag = {"DEBUG": "INFO", "INFO": "INFO", "WARNING": "WARN",
               "ERROR": "ERROR", "CRITICAL": "ERROR"}.get(level, "INFO")
        style = {"WARNING": "yellow", "ERROR": "red",
                 "CRITICAL": "red"}.get(level, "bright_black")
        line = Text()
        line.append(f"[{time.strftime('%H:%M:%S')}] ", style="bright_black")
        line.append(f"{tag.rjust(5)} ", style=style)
        line.append(Text(msg.content))
        self._append_thought(line)

    # ---- 确认弹窗 ----
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
        self.query_one("#main-log", RichLog).write(renderable)

    def _append_terminal(self, renderable) -> None:
        self.query_one("#terminal-log", RichLog).write(renderable)
        self._terminal_lines.append(renderable)
        if len(self._terminal_lines) > 2000:
            self._terminal_lines = self._terminal_lines[-2000:]

    def _append_diff_event(self, data: dict) -> None:
        """把文件写操作渲染成类 diff 行，进入文件变更视图。"""
        tool = data.get("tool", "")
        params = data.get("params", {}) or {}
        action = params.get("action", "")
        if tool == "file_ops" and action in ("write", "append"):
            self._append_diff(Text(
                f"{params.get('path', '')} ({action})",
                style="bold yellow"))
            content = str(params.get("content", ""))
            for line in content.splitlines()[:60]:
                self._append_diff(Text(f"  + {line}", style="green"))
        else:
            self._append_diff(Text(
                f"{tool} {_short(params, 120)}", style="dim"))

    def _append_diff(self, renderable) -> None:
        self.query_one("#diff-log", RichLog).write(renderable)

    # ---- 布局 ----
    def _is_compact(self) -> bool:
        if self._layout_override == "compact":
            return True
        if self._layout_override == "wide":
            return False
        width = self.size.width
        if width == 0:  # 挂载初期尺寸未就绪，按宽屏处理
            return False
        return width < _NARROW_WIDTH

    def _apply_layout(self) -> None:
        compact = self._is_compact() or not self._task_panel_visible
        self.query_one("#task-panel", Static).display = not compact
        self.query_one("#compact-header", Static).display = compact

    # ---- 状态刷新（0.5s 定时 + 事件触发） ----
    def refresh_status(self) -> None:
        if not self.is_mounted:  # 挂载完成前定时器/事件可能先触发
            return
        self._update_task_panel()
        self._update_status_bar()
        self._update_compact_header()
        self.refresh_views()

    def _update_task_panel(self) -> None:
        panel = self.query_one("#task-panel", Static)
        runner = self.runner
        loop = runner.loop if runner else None
        phase = loop.state.phase.value if loop else "idle"
        lines = [f"[bold]任务[/bold]: {self.prompt[:26]}"]
        lines.append("-" * 26)
        lines.append(f"[bold]阶段[/bold]: {_phase_text(phase)}")
        lines.append("任务树:")
        tasks = list(loop.scheduler.dag.all()) if loop else []
        if tasks:
            done = sum(1 for t in tasks if t.status.value == "completed")
            for t in tasks[:8]:
                lines.append(_task_row(t))
            if len(tasks) > 8:
                lines.append(f"  ... 共 {len(tasks)} 项")
            total = len(tasks) or 1
            pct = int(done / total * 100)
            lines.append(f"进度: {_progress_bar(pct)} {pct}%")
        else:
            lines.append("  （暂无任务）")
        lines.append(f"耗时: {_fmt_elapsed(time.monotonic() - self._started_at)}")
        panel.update("\n".join(lines))

    def _update_status_bar(self) -> None:
        bar = self.query_one("#status-bar", Static)
        runner = self.runner
        loop = runner.loop if runner else None
        tokens = self._estimate_tokens()
        max_tokens = 8000
        if self.config is not None:
            ctx = getattr(self.config, "context", None)
            if ctx is not None and getattr(ctx, "max_tokens", 0):
                max_tokens = ctx.max_tokens
        t_pct = tokens / max_tokens if max_tokens else 0
        t_style = "yellow" if t_pct >= 0.8 else ("red" if t_pct >= 0.95 else "white")
        rounds = runner.total_rounds() if runner else 0
        max_rounds = getattr(loop, "_max_rounds", 0) if loop else 0
        r_style = "yellow" if (max_rounds and rounds / max_rounds >= 0.9) else "white"
        bar.update(
            f"tokens: [{t_style}]{tokens:,}[/] | "
            f"round: [{r_style}]{rounds}/{max_rounds}[/] | "
            f"mem: {self._memory_usage()} | session: {self._session_id}"
        )

    def _update_compact_header(self) -> None:
        header = self.query_one("#compact-header", Static)
        runner = self.runner
        loop = runner.loop if runner else None
        phase = loop.state.phase.value if loop else "idle"
        rounds = runner.total_rounds() if runner else 0
        summary = runner.dag_summary().get("by_status", {}) if runner else {}
        done = summary.get("completed", 0)
        total = sum(summary.values())
        header.update(
            f"阶段: {phase.upper()} | 任务: {done}/{total} | 轮次: {rounds}")

    def _memory_usage(self) -> str:
        runner = self.runner
        if runner is None or runner.loop is None:
            return "-"
        mem = getattr(runner.loop, "memory", None)
        if mem is None:
            return "-"
        try:
            if getattr(mem, "disabled", False):
                return "off"
            conn = getattr(mem, "_conn", None)
            if conn is not None:
                max_entities = getattr(mem, "max_entities", None) or 1000
                cur = conn.execute("SELECT COUNT(*) FROM memories")
                count = int(cur.fetchone()[0])
                return f"{int(count / max_entities * 100)}%"
        except Exception:
            pass
        return "-"

    def refresh_views(self) -> None:
        """刷新监控视图（主区 F5 视图之一）。"""
        runner = self.runner
        if runner is None or runner.loop is None:
            return
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
            lines.extend(a for a in alerts)
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

    # ---- 输入栏 ----
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if getattr(event.input, "id", None) != "input-bar":
            return
        box = self.query_one("#input-bar", CommandInput)
        value = box.value.strip()
        box.value = ""
        if not value:
            return
        self._push_history(value)
        if value.startswith("/"):
            self._handle_command(value)
        else:
            self._inject(value)

    def _push_history(self, value: str) -> None:
        if not self._input_history or self._input_history[-1] != value:
            self._input_history.append(value)
        self._hist_idx = None

    def input_history_prev(self) -> None:
        if not self._input_history:
            return
        if self._hist_idx is None:
            self._hist_idx = len(self._input_history)
        if self._hist_idx > 0:
            self._hist_idx -= 1
            self.query_one("#input-bar", CommandInput).value = (
                self._input_history[self._hist_idx])

    def input_history_next(self) -> None:
        box = self.query_one("#input-bar", CommandInput)
        if self._hist_idx is None:
            return
        self._hist_idx += 1
        if self._hist_idx >= len(self._input_history):
            self._hist_idx = None
            box.value = ""
        else:
            box.value = self._input_history[self._hist_idx]

    def _handle_command(self, raw: str) -> None:
        cmd, _, rest = raw.partition(" ")
        cmd = cmd.lower()
        runner = self.runner
        loop = runner.loop if runner else None
        if cmd == "/pause":
            if loop is not None:
                loop.pause()
            self._append_thought(Text("已暂停（Ctrl+P 恢复）", style="yellow"))
        elif cmd == "/resume":
            if loop is not None:
                loop.resume()
            self._append_thought(Text("已恢复运行", style="green"))
        elif cmd == "/status":
            self._append_status_line()
        elif cmd == "/retry":
            self._inject("重试当前步骤" + (f"：{rest}" if rest else ""))
        elif cmd == "/skip":
            self._inject("跳过当前步骤" + (f"：{rest}" if rest else ""))
        elif cmd == "/quit":
            self.exit()
        else:
            self._append_thought(
                Text(f"未知命令: {cmd}（F1 查看帮助）", style="yellow"))
        self.refresh_status()

    def _append_status_line(self) -> None:
        runner = self.runner
        loop = runner.loop if runner else None
        phase = loop.state.phase.value if loop else "-"
        rounds = runner.total_rounds() if runner else 0
        tokens = self._estimate_tokens()
        tasks = runner.dag_summary().get("by_status", {}) if runner else {}
        bits = " ".join(f"{k}:{v}" for k, v in sorted(tasks.items()))
        self._append_thought(Text(
            f"状态: 阶段={phase} 轮次={rounds} tokens={tokens} "
            f"任务[{bits}] 会话={self._session_id}",
            style="bright_black"))

    def _inject(self, value: str) -> None:
        runner = self.runner
        if runner is None or runner.loop is None:
            self._append_thought(
                Text("Agent 尚未就绪，无法注入指令", style="yellow"))
            return
        runner.loop.interrupt(value)
        self._append_thought(format_event(
            {"type": "interrupt", "data": {"prompt": value}}))
        self.refresh_status()

    # ---- 动作 ----
    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_toggle_task_panel(self) -> None:
        self._task_panel_visible = not self._task_panel_visible
        self._apply_layout()

    def action_show_terminal(self) -> None:
        self.push_screen(TerminalScreen(self._terminal_lines))

    def action_toggle_layout(self) -> None:
        if self._layout_override is None:
            self._layout_override = "wide" if self._is_compact() else "compact"
        else:
            self._layout_override = None
        self._apply_layout()

    def action_cycle_main_view(self) -> None:
        idx = _MAIN_VIEWS.index(self._main_view)
        self._main_view = _MAIN_VIEWS[(idx + 1) % len(_MAIN_VIEWS)]
        self.query_one("#main-log", RichLog).display = self._main_view == "log"
        self.query_one("#diff-log", RichLog).display = self._main_view == "diff"
        self.query_one("#metrics-view", Static).display = (
            self._main_view == "metrics")

    def action_focus_input(self) -> None:
        box = self.query_one("#input-bar", CommandInput)
        box.focus()
        self._append_thought(
            Text("输入指令覆盖当前任务，Enter 提交（/ 开头为命令）",
                 style="bright_black"))

    def action_toggle_pause(self) -> None:
        runner = self.runner
        if runner is None or runner.loop is None:
            return
        if runner.loop.paused:
            runner.loop.resume()
            self._append_thought(Text("已恢复运行", style="green"))
        else:
            runner.loop.pause()
            self._append_thought(Text("已暂停（Ctrl+P 恢复）", style="yellow"))
        self.refresh_status()

    def action_retry_step(self) -> None:
        self._inject("重试当前步骤")

    def action_skip_step(self) -> None:
        self._inject("跳过当前步骤，继续后续工作")

    def action_clear_terminal(self) -> None:
        self.query_one("#terminal-log", RichLog).clear()
        self._terminal_lines = []


# ---- 渲染辅助（纯函数，便于单测） ----
def _phase_text(phase: str) -> str:
    color = _PHASE_COLORS.get(phase, "white")
    return f"[{color}]{phase.upper()}[/{color}]"


def _task_row(t: Any) -> str:
    st = t.status.value
    mark, color = _TASK_MARKS.get(st, ("未知 ", "bright_black"))
    label = f"{t.instruction[:22]}"
    meta = t.metadata or {}
    if meta.get("skill"):
        idx = (meta.get("step_index") or 0) + 1
        total = meta.get("step_total") or 0
        label += f" [{meta.get('skill_step')} {idx}/{total}]"
    return f"[{color}]{mark}[/{color}] {label}"


def _progress_bar(pct: int) -> str:
    w = 20
    pct = max(0, min(100, int(pct)))
    filled = int(pct / 100 * w)
    if filled >= w:
        return "[" + "=" * w + "]"
    return "[" + "=" * filled + ">" + "-" * (w - filled - 1) + "]"


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _short(value: Any, limit: int) -> str:
    text = str(value).replace("\n", "\\n ")
    return text if len(text) <= limit else text[:limit] + "..."


__all__ = ["AlphaSWEApp", "CommandInput", "ConfirmationScreen"]
