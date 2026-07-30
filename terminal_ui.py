"""第七关：Terminal UI 可视化仪表盘
使用 rich 库搭建实时仪表盘：
- 左侧：当前 Step Number、Loop 状态（思考中/执行中/解析中）
- 右侧：Token 消耗、最后一次 Tool Call 参数/返回值（截断）
"""
from __future__ import annotations
import logging
import time
import threading
from typing import Optional, Dict, Any

logger = logging.getLogger("alpha-swe.terminal_ui")

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


class TerminalUI:
    """Rich 终端仪表盘"""

    def __init__(self, refresh_rate: int = 4):
        if not HAS_RICH:
            logger.warning("rich 未安装，Terminal UI 不可用。请执行: pip install rich")
            self.console = None
            return

        self.console = Console()
        self.refresh_rate = refresh_rate
        self.live: Optional[Live] = None

        # 仪表盘数据
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
        }
        self._lock = threading.Lock()

    def start(self):
        """启动仪表盘"""
        if not HAS_RICH:
            return
        import rich.live
        self.live = rich.live.Live(
            self._render(),
            console=self.console,
            refresh_per_second=self.refresh_rate,
            screen=True
        )
        self.live.start()

    def stop(self):
        """停止仪表盘"""
        if self.live:
            self.live.stop()

    def update(self, **kwargs):
        """更新仪表盘数据"""
        with self._lock:
            self.data.update(kwargs)

    def update_status(self, status: str):
        """更新状态"""
        colors = {
            "idle": "white",
            "thinking": "yellow",
            "executing": "cyan",
            "parsing": "magenta",
            "done": "green",
            "error": "red"
        }
        self.update(
            status=status,
            status_color=colors.get(status, "white")
        )

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
        """渲染状态面板"""
        d = self.data
        status_text = Text()
        status_text.append("Alpha-SWE 状态\n", style="bold underline")
        status_text.append(f"\n步骤: {d['step']}")
        status_text.append(f"\n状态: ", style="white")
        status_text.append(d["status"], style=f"bold {d['status_color']}")
        status_text.append(f"\n当前操作: {d['current_action']}")
        return Panel(status_text, title="Loop 状态", border_style="cyan")

    def _render_plan(self) -> Panel:
        """渲染计划栈"""
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
        """渲染错误面板"""
        d = self.data
        err_text = Text("", style="red")
        if d["errors"]:
            for e in d["errors"][-5:]:
                err_text.append(f"\n• {str(e)[:80]}")
        else:
            err_text.append("(无错误)", style="green")
        return Panel(err_text, title="错误日志", border_style="red")

    def _render_tokens(self) -> Panel:
        """渲染 Token 消耗"""
        d = self.data
        token_text = Text()
        token_text.append("Token 消耗\n", style="bold underline")

        # 进度条
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

        # 水位线
        watermark = d.get("watermark", "")
        if watermark:
            token_text.append(f"\n水位: {watermark}")

        return Panel(token_text, title="Token 消耗", border_style="green")

    def _render_tool_call(self) -> Panel:
        """渲染最后一次工具调用"""
        d = self.data
        tool_text = Text()
        tool_text.append("最后一次 Tool Call\n", style="bold underline")

        if d["last_tool_call"]:
            params_str = str(d["last_tool_call"])
            if len(params_str) > 100:
                params_str = params_str[:50] + "..." + params_str[-50:]
            tool_text.append(f"\n参数: {params_str}")

        if d["last_tool_result"]:
            result_str = str(d["last_tool_result"])
            if len(result_str) > 100:
                result_str = result_str[:50] + "..." + result_str[-50:]
            tool_text.append(f"\n结果: {result_str}")

        return Panel(tool_text, title="Tool Call", border_style="blue")

    def _render_stats(self) -> Panel:
        """渲染统计面板"""
        d = self.data
        stats_text = Text()
        stats_text.append(f"沙箱拦截: {d['sandbox_violations']}")
        stats_text.append(f"\n压缩次数: {d['compression_count']}")
        stats_text.append(f"\n记忆实体: {d['memory_entities']}")
        stats_text.append(f"\n后台任务: {d['bg_tasks']}")
        return Panel(stats_text, title="统计数据", border_style="magenta")

    def render_ascii(self) -> str:
        """ASCII 文本布局（当 rich 不可用时）"""
        d = self.data
        lines = [
            "=" * 70,
            "  Alpha-SWE Terminal Dashboard",
            "=" * 70,
            f"  Step: {d['step']}  |  Status: {d['status']}  |  Action: {d['current_action']}",
            "-" * 70,
            f"  Token: {d['token_used']:,}/{d['token_limit']:,} ({d['token_percent']})",
            f"  Watermark: {d.get('watermark', 'N/A')}",
            f"  Last Tool Call: {str(d['last_tool_call'])[:70]}",
            f"  Last Tool Result: {str(d['last_tool_result'])[:70]}",
            "-" * 70,
            f"  Plan Stack: {d['plan_stack'][-3:] if d['plan_stack'] else 'N/A'}",
            f"  Errors: {d['errors'][-2:] if d['errors'] else 'None'}",
            "-" * 70,
            f"  Sandbox Violations: {d['sandbox_violations']} | Compressions: {d['compression_count']}",
            f"  Memory Entities: {d['memory_entities']} | BG Tasks: {d['bg_tasks']}",
            "=" * 70,
        ]
        return "\n".join(lines)