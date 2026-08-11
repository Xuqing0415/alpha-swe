"""火焰图/时间线视图 —— 把 span 数据渲染成 ASCII 时间线。

- 横向模式（宽屏）：顶部时间轴刻度，每行一条 `#` 条，右侧耗时；
- 纵向瀑布模式（窄屏）：每行 `[####]` 条，底部汇总；
- 颜色：llm/think 青、tool 亮白、task 白、error 红。
纯 ASCII 输出，无 emoji。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from rich.text import Text
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static

_KIND_STYLES = {
    "run": "bold white",
    "task": "white",
    "llm": "cyan",
    "tool": "bold white",
}
_TICKS = (0, 0.25, 0.5, 0.75, 1.0)


def _fmt_time(t: float) -> str:
    return f"{t:.1f}s"


def _style(kind: str, status: str) -> str:
    if status == "error":
        return "red"
    return _KIND_STYLES.get(kind, "")


def _total(rows: List[Dict[str, Any]]) -> float:
    return max((r["start"] + r["duration"]) for r in rows) if rows else 0.0


def summarize(rows: List[Dict[str, Any]]) -> str:
    """底部汇总：总耗时 / 步数 / 最慢步骤。"""
    total = _total(rows)
    parts = [f"总耗时: {_fmt_time(total)}", f"步数: {len(rows)}"]
    if rows:
        slowest = max(rows, key=lambda r: r["duration"])
        parts.append(f"最慢: {slowest['name']} ({_fmt_time(slowest['duration'])})")
    return " | ".join(parts)


def _span_bar(row: Dict[str, Any], width: int) -> str:
    total = max(_total([row]), 1e-9)
    x0 = int(row["start"] / total * width)
    x1 = int((row["start"] + row["duration"]) / total * width)
    if x1 <= x0:
        x1 = x0 + 1
    cells = [" "] * width
    for i in range(max(x0, 0), min(x1, width)):
        cells[i] = "#"
    return "".join(cells)


def render_horizontal(rows: List[Dict[str, Any]], *,
                      name_width: int = 24,
                      axis_width: int = 48,
                      selected: Optional[int] = None) -> List[Text]:
    """横向时间线：时间轴刻度 + 每行 # 条。"""
    total = _total(rows)
    lines: List[Text] = []

    labels = [" "] * axis_width
    axis = [" "] * axis_width
    for frac in _TICKS:
        x = int(frac * (axis_width - 1))
        axis[x] = "|"
        label = _fmt_time(total * frac)
        start = x if x + len(label) <= axis_width else axis_width - len(label)
        for i, ch in enumerate(label):
            if start + i < axis_width:
                labels[start + i] = ch
    prev = -1
    for frac in _TICKS:
        x = int(frac * (axis_width - 1))
        for i in range(prev + 1, x):
            axis[i] = "-"
        prev = x

    lines.append(Text(" " * name_width + "".join(labels).rstrip(),
                      style="bright_black"))
    lines.append(Text(" " * name_width + "".join(axis), style="bright_black"))
    for i, r in enumerate(rows):
        style = _style(r.get("kind", ""), r.get("status", ""))
        row = Text()
        marker = ">" if selected == i else " "
        row.append(marker + r["name"][:name_width - 1].ljust(name_width - 1),
                   style=style)
        if total > 0:
            x0 = int(r["start"] / total * axis_width)
            x1 = int((r["start"] + r["duration"]) / total * axis_width)
            if x1 <= x0:
                x1 = x0 + 1
            cells = [" "] * axis_width
            for j in range(max(x0, 0), min(x1, axis_width)):
                cells[j] = "#"
            row.append("".join(cells), style=style)
        row.append(f"  {_fmt_time(r['duration'])}", style="bright_black")
        if selected == i:
            row.stylize("reverse")
        lines.append(row)
    lines.append(Text(summarize(rows), style="bright_black"))
    return lines


def render_waterfall(rows: List[Dict[str, Any]], *,
                     name_width: int = 22,
                     bar_width: int = 40,
                     selected: Optional[int] = None) -> List[Text]:
    """纵向瀑布图：每行 [####] 条 + 底部汇总（窄屏）。"""
    total = _total(rows)
    lines: List[Text] = []
    for i, r in enumerate(rows):
        style = _style(r.get("kind", ""), r.get("status", ""))
        bar = _span_bar(r, bar_width) if total > 0 else " " * bar_width
        row = Text()
        marker = ">" if selected == i else " "
        row.append(marker + r["name"][:name_width - 1].ljust(name_width - 1),
                   style=style)
        row.append("[" + bar + "]", style=style)
        row.append(f" {_fmt_time(r['duration'])}", style="bright_black")
        if selected == i:
            row.stylize("reverse")
        lines.append(row)
    lines.append(Text("-" * (name_width + bar_width + 3), style="bright_black"))
    lines.append(Text(summarize(rows), style="bright_black"))
    return lines


def render_timeline(rows: List[Dict[str, Any]], *,
                    width: int = 100, narrow: bool = False,
                    selected: Optional[int] = None) -> List[Text]:
    """按屏幕宽度选择横向或纵向渲染。"""
    if narrow:
        return render_waterfall(rows, bar_width=max(20, min(50, width - 30)),
                                selected=selected)
    axis = max(30, min(110, width - 34))
    return render_horizontal(rows, axis_width=axis, selected=selected)


__all__ = ["render_timeline", "render_horizontal", "render_waterfall",
           "summarize", "TimelineView", "TimelineDetailScreen"]


class TimelineView(Static):
    """可交互时间线：上/下选中 span，Enter 展开详情。

    渲染复用 render_timeline，选中行加 `>` 标记并以反转色高亮。
    """

    can_focus = True

    BINDINGS = [
        Binding("up", "select_prev", "上移", show=False),
        Binding("down", "select_next", "下移", show=False),
        Binding("enter", "show_detail", "详情", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__("", markup=True, **kwargs)
        self._rows: List[Dict[str, Any]] = []
        self._selected = 0
        self._width = 100
        self._narrow = False

    def set_rows(self, rows, *, width=100, narrow=False) -> None:
        self._rows = list(rows)
        self._width = width
        self._narrow = narrow
        if self._rows:
            self._selected = max(0, min(self._selected,
                                        len(self._rows) - 1))
        else:
            self._selected = 0
        self._render_lines()

    def _render_lines(self) -> None:
        lines = render_timeline(self._rows, width=self._width,
                                narrow=self._narrow,
                                selected=self._selected)
        combined = Text()
        for line in lines:
            combined.append_text(line)
            combined.append("\n")
        self.update(combined)

    def action_select_prev(self) -> None:
        if self._rows and self._selected > 0:
            self._selected -= 1
            self._render_lines()

    def action_select_next(self) -> None:
        if self._rows and self._selected < len(self._rows) - 1:
            self._selected += 1
            self._render_lines()

    def action_show_detail(self) -> None:
        if not self._rows:
            return
        host = self.app
        if hasattr(host, "show_timeline_detail"):
            host.show_timeline_detail(self._rows[self._selected])


class TimelineDetailScreen(ModalScreen[None]):
    """span 详情弹窗：名称/类型/耗时/参数/输出摘要/错误。"""

    BINDINGS = [Binding("escape", "close_detail", "关闭")]

    def __init__(self, row: Dict[str, Any], **kwargs):
        super().__init__(**kwargs)
        self.row = row

    def compose(self):
        yield Static(self._render_text(), id="timeline-detail-box", markup=True)

    def _render_text(self) -> str:
        r = self.row
        lines = [
            "[bold]Span 详情[/bold]",
            f"名称: {r.get('name', '')}",
            f"类型: {r.get('kind', '')}    状态: {r.get('status', '')}",
            f"耗时: {r.get('duration', 0.0):.3f}s    "
            f"起始: {r.get('start', 0.0):.2f}s",
        ]
        if r.get("params"):
            lines.append(f"参数: {r['params']}")
        if r.get("out"):
            lines.append(f"输出: {r['out']}")
        if r.get("error"):
            lines.append(f"错误: {r['error']}")
        lines.append("")
        lines.append("[bright_black]Esc 关闭[/bright_black]")
        return "\n".join(lines)

    def action_close_detail(self) -> None:
        self.app.pop_screen()
