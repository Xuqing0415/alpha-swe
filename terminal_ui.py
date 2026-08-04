"""第七关：Terminal UI 可视化仪表盘（解耦版）
通过 EventBus 消费事件，UI 线程永不阻塞 Worker 核心逻辑。
"""
import logging
import threading
import queue as std_queue

from event_bus import event_bus, AgentEvent

logger = logging.getLogger("alpha-swe.terminal_ui")

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


class TerminalUI:
    """Rich 终端仪表盘（事件驱动，解耦版）"""

    def __init__(self, refresh_rate: int = 4):
        self.refresh_rate = refresh_rate
        self.console = Console() if HAS_RICH else None
        self.live = None
        self._running = False
        self._ui_thread = None

        if not HAS_RICH:
            logger.warning("rich 未安装，Terminal UI 不可用。请执行: pip install rich")

        # 仪表盘数据（无 rich 时也初始化，保证 update()/update_status() 安全）
        self.data = {
            "step": "0/0",
            "status": "idle",
            "status_color": "white",
            "current_action": "",
            "token_used": 0,
            "token_limit": 100000,
            "token_percent": "0%",
            "watermark": "",
            "last_tool_call": "",
            "last_tool_result": "",
            "plan_stack": [],
            "errors": [],
            "sandbox_violations": 0,
            "compression_count": 0,
            "memory_entities": 0,
            "bg_tasks": 0,
            "queue_depth": 0,
            "retry_count": 0,
            "trace_id": "",
        }
        self._lock = threading.Lock()

    def update_status(self, status: str):
        """更新状态（线程安全；无 rich 时为安全的空操作）"""
        colors = {"idle": "white", "thinking": "yellow", "executing": "cyan",
                  "parsing": "magenta", "done": "green", "error": "red"}
        with self._lock:
            self.data["status"] = status
            self.data["status_color"] = colors.get(status, "white")

    def update(self, **kwargs):
        """更新仪表盘字段（线程安全；无 rich 时为安全的空操作）"""
        with self._lock:
            for key, value in kwargs.items():
                if key in self.data:
                    self.data[key] = value

    def start(self):
        """启动 UI 线程（独立于 Worker）"""
        if not HAS_RICH:
            return

        self._running = True
        self._ui_thread = threading.Thread(target=self._ui_loop, daemon=True)
        self._ui_thread.start()
        logger.info("Terminal UI 已启动（事件驱动模式）")

    def stop(self):
        """停止 UI"""
        self._running = False
        if self._ui_thread:
            self._ui_thread.join(timeout=2.0)

    def _ui_loop(self):
        """UI 主循环：消费事件 -> 刷新布局（永不阻塞 Worker）"""
        import rich.live

        self.live = rich.live.Live(
            self._render(),
            console=self.console,
            refresh_per_second=self.refresh_rate,
            screen=True
        )
        self.live.start()

        try:
            while self._running:
                # 非阻塞消费事件
                try:
                    event = event_bus.consume(timeout=0.05)
                    if event:
                        self._handle_event(event)
                except std_queue.Empty:
                    pass

                self.live.update(self._render())
        finally:
            self.live.stop()

    def _handle_event(self, event: AgentEvent):
        """处理事件，更新仪表盘数据"""
        with self._lock:
            etype = event.event_type
            data = event.data

            if etype == "step_start":
                self.data["step"] = f"{data.get('step_index', 0) + 1}/{data.get('total_steps', 0)}"
                self.data["status"] = "executing"
                self.data["status_color"] = "cyan"
                self.data["current_action"] = data.get("description", "")[:50]

            elif etype == "step_end":
                self.data["status"] = "parsing"
                self.data["status_color"] = "magenta"

            elif etype == "tool_call":
                params_str = str(data.get("params", {}))
                self.data["last_tool_call"] = params_str[:100]
                self.data["status"] = "executing"
                self.data["status_color"] = "cyan"

            elif etype == "tool_result":
                result_str = str(data.get("output", ""))
                self.data["last_tool_result"] = result_str[:100]
                if data.get("success"):
                    self.data["errors"] = self.data["errors"][-4:]  # keep last 4
                else:
                    self.data["errors"].append(data.get("error", "unknown")[:80])

            elif etype == "error":
                self.data["errors"].append(data.get("message", "unknown")[:80])
                self.data["status"] = "error"
                self.data["status_color"] = "red"

            elif etype == "compress":
                self.data["compression_count"] = data.get("count", self.data["compression_count"])
                self.data["watermark"] = f"压缩 #{data.get('count', 0)}"

            elif etype == "status":
                colors = {"idle": "white", "thinking": "yellow", "executing": "cyan",
                          "parsing": "magenta", "done": "green", "error": "red"}
                s = data.get("status", "idle")
                self.data["status"] = s
                self.data["status_color"] = colors.get(s, "white")

            elif etype == "token_update":
                self.data["token_used"] = data.get("used", 0)
                self.data["token_limit"] = data.get("limit", 100000)
                pct = data.get("percent", 0)
                self.data["token_percent"] = f"{pct:.1f}%"

            elif etype == "sandbox_violation":
                self.data["sandbox_violations"] = data.get("count", self.data["sandbox_violations"] + 1)

            elif etype == "memory_update":
                self.data["memory_entities"] = data.get("count", 0)

            elif etype == "bg_task":
                self.data["bg_tasks"] = data.get("active", 0)

            elif etype == "retry":
                self.data["retry_count"] = data.get("count", self.data["retry_count"] + 1)

            elif etype == "trace":
                self.data["trace_id"] = data.get("trace_id", "")[:8]

            # 通用：计划栈
            if "plan_stack" in data:
                self.data["plan_stack"] = data["plan_stack"]

            # 队列深度
            self.data["queue_depth"] = event_bus.size()

    def _render(self):
        """渲染布局"""
        import rich.layout
        layout = rich.layout.Layout()
        layout.split_row(
            rich.layout.Layout(name="left", ratio=2),
            rich.layout.Layout(name="right", ratio=3)
        )

        layout["left"].split_column(
            rich.layout.Layout(self._render_status(), name="status"),
            rich.layout.Layout(self._render_plan(), name="plan"),
            rich.layout.Layout(self._render_errors(), name="errors")
        )

        layout["right"].split_column(
            rich.layout.Layout(self._render_tokens(), name="tokens"),
            rich.layout.Layout(self._render_tool_call(), name="tool_call"),
            rich.layout.Layout(self._render_stats(), name="stats")
        )

        return layout

    def _render_status(self) -> Panel:
        d = self.data
        status_text = Text()
        status_text.append("Alpha-SWE 状态\n", style="bold underline")
        status_text.append(f"\n步骤: {d['step']}")
        status_text.append(f"\n状态: ", style="white")
        status_text.append(d["status"], style=f"bold {d['status_color']}")
        status_text.append(f"\n当前操作: {d['current_action']}")
        if d.get("trace_id"):
            status_text.append(f"\nTrace: {d['trace_id']}")
        return Panel(status_text, title="Loop 状态", border_style="cyan")

    def _render_plan(self) -> Panel:
        d = self.data
        plan_text = Text("计划栈:\n", style="bold")
        if d["plan_stack"]:
            for i, p in enumerate(d["plan_stack"][-8:]):
                icon = "✓" if "done" in str(p).lower() else "○"
                plan_text.append(f"\n{icon} {str(p)[:60]}")
        else:
            plan_text.append("\n(暂无计划)")
        return Panel(plan_text, title="当前计划栈", border_style="yellow")

    def _render_errors(self) -> Panel:
        d = self.data
        err_text = Text("", style="red")
        if d["errors"]:
            for e in d["errors"][-5:]:
                err_text.append(f"\n• {str(e)[:80]}")
        else:
            err_text.append("(无错误)", style="green")
        return Panel(err_text, title="错误日志", border_style="red")

    def _render_tokens(self) -> Panel:
        d = self.data
        token_text = Text()
        token_text.append("Token 消耗\n", style="bold underline")
        bar_len = 30
        percent = 0
        try:
            percent = float(d["token_percent"].replace("%", ""))
        except (ValueError, AttributeError):
            pass
        filled = int(bar_len * percent / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        token_text.append(f"\n[{bar}] {d['token_percent']}")
        token_text.append(f"\n已用: {d['token_used']:,} / {d['token_limit']:,}")
        watermark = d.get("watermark", "")
        if watermark:
            token_text.append(f"\n水位: {watermark}")
        return Panel(token_text, title="Token 消耗", border_style="green")

    def _render_tool_call(self) -> Panel:
        d = self.data
        tool_text = Text()
        tool_text.append("最后一次 Tool Call\n", style="bold underline")
        if d["last_tool_call"]:
            s = str(d["last_tool_call"])
            if len(s) > 100:
                s = s[:50] + "..." + s[-50:]
            tool_text.append(f"\n参数: {s}")
        if d["last_tool_result"]:
            s = str(d["last_tool_result"])
            if len(s) > 100:
                s = s[:50] + "..." + s[-50:]
            tool_text.append(f"\n结果: {s}")
        return Panel(tool_text, title="Tool Call", border_style="blue")

    def _render_stats(self) -> Panel:
        d = self.data
        stats_text = Text()
        stats_text.append(f"沙箱拦截: {d['sandbox_violations']}")
        stats_text.append(f"\n压缩次数: {d['compression_count']}")
        stats_text.append(f"\n记忆实体: {d['memory_entities']}")
        stats_text.append(f"\n后台任务: {d['bg_tasks']}")
        stats_text.append(f"\n重试次数: {d['retry_count']}")
        stats_text.append(f"\n队列深度: {d['queue_depth']}")
        return Panel(stats_text, title="统计数据", border_style="magenta")

    def render_ascii(self) -> str:
        d = self.data
        lines = [
            "=" * 70,
            "  Alpha-SWE Terminal Dashboard",
            "=" * 70,
            f"  Step: {d['step']}  |  Status: {d['status']}  |  Action: {d['current_action']}",
            f"  Trace: {d.get('trace_id', 'N/A')}",
            "-" * 70,
            f"  Token: {d['token_used']:,}/{d['token_limit']:,} ({d['token_percent']})",
            f"  Watermark: {d.get('watermark', 'N/A')}",
            f"  Last Tool Call: {str(d['last_tool_call'])[:70]}",
            f"  Last Tool Result: {str(d['last_tool_result'])[:70]}",
            "-" * 70,
            f"  Plan Stack: {d['plan_stack'][-3:] if d['plan_stack'] else 'N/A'}",
            f"  Errors: {d['errors'][-2:] if d['errors'] else 'None'}",
            "-" * 70,
            f"  Sandbox: {d['sandbox_violations']} | Compression: {d['compression_count']}",
            f"  Memory: {d['memory_entities']} | BG Tasks: {d['bg_tasks']} | Retries: {d['retry_count']}",
            f"  Queue Depth: {d['queue_depth']}",
            "=" * 70,
        ]
        return "\n".join(lines)