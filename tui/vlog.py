"""主日志区虚拟滚动组件 —— 基于 Textual DataTable 的三列日志视图。

设计（对应纯终端 UI 方案的“虚拟滚动”一节）：
- DataTable 内置虚拟滚动：只渲染可见行，万行级日志保持流畅；
- 三列：时间戳 / 类型 / 内容，前两列固定列宽，内容列随文本自适应；
- 环形缓冲：超过 max_lines 时批量裁剪最旧行（避免逐行 O(n) 删除）；
- 自动跟随：写入时滚到底部；向上滚动进入“浏览中”，End 或滚到底恢复跟随。
"""
from __future__ import annotations

import re
import time
from typing import Any, List

from rich.text import Span, Text
from textual.binding import Binding
from textual.widgets import DataTable

_PREFIX_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] +([A-Z]+) +")

_TAG_STYLES = {
    "THINK": "cyan",
    "ACT": "bold white",
    "OBS": "",
    "OK": "green",
    "WARN": "yellow",
    "ERROR": "red",
    "INFO": "bright_black",
    "MEM": "bright_black",
}

# 环形缓冲裁剪批量：避免每次写入都触发 O(n) 的 remove_row
_PRUNE_BATCH = 1000


def _slice_text(text: Text, start: int) -> Text:
    """返回从 start（字符偏移）开始的子 Text，保留样式 span，去掉末尾换行。"""
    out = Text()
    out.style = text.style
    parts: list[str] = []
    remaining = start
    for seg in text._text:
        if remaining <= 0:
            parts.append(seg)
            continue
        seg_len = len(seg)
        if remaining < seg_len:
            parts.append(seg[remaining:])
            remaining = 0
        else:
            remaining -= seg_len
    if parts:
        parts[-1] = parts[-1].rstrip("\n")
    out._text = parts
    spans = []
    plain_len = len("".join(parts))
    for sp in text._spans:
        s = max(sp.start - start, 0)
        e = sp.end - start
        if e <= 0 or s >= plain_len:
            continue
        spans.append(Span(s, min(e, plain_len), sp.style))
    out._spans = spans
    return out


class VirtualLog(DataTable[Any]):
    """三列虚拟滚动日志区（时间戳 / 类型 / 内容）。

    对外保持 RichLog 风格的 ``write(renderable)`` 接口与 ``lines`` 属性，
    兼容既有事件渲染与测试访问；内部使用 DataTable 的虚拟滚动。
    """

    BINDINGS = [
        Binding("end", "follow_end", "到底部", show=False),
    ]

    def __init__(
        self,
        *,
        max_lines: int = 10000,
        ts_width: int = 10,
        tag_width: int = 6,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            cursor_type="none",
            show_header=False,
            show_row_labels=False,
            zebra_stripes=False,
            **kwargs,
        )
        self._max_lines = max_lines
        self._follow = True  # 自动跟随模式（新行写入滚到底部）
        self._rows: List[Any] = []  # 原始 renderable，供 lines 属性使用
        self._keys: List[Any] = []
        self.add_column("时间", key="ts", width=ts_width)
        self.add_column("类型", key="tag", width=tag_width)
        self.add_column("内容", key="content")

    # ---- 兼容接口 ----
    @property
    def lines(self) -> List[Any]:
        """逐行 renderable（兼容旧 RichLog.lines 的测试与调用）。"""
        return list(self._rows)

    @property
    def follow(self) -> bool:
        return self._follow

    # ---- 写入 ----
    def write(self, renderable: Any) -> None:
        """追加一行日志。支持带 `[HH:MM:SS] TYPE 内容` 前缀的 Text。"""
        text = renderable if isinstance(renderable, Text) else Text(str(renderable))
        ts, tag, body = self._split(text)
        height = None if "\n" in body.plain else 1
        key = self.add_row(
            Text(ts, style="bright_black"),
            Text(tag, style=_TAG_STYLES.get(tag, "")),
            body,
            height=height,
        )
        self._rows.append(text)
        self._keys.append(key)
        self._prune()
        if self._follow and self.is_mounted:
            self.scroll_end(animate=False)

    def clear(self) -> None:  # noqa: D102  # 与 RichLog 接口对齐
        super().clear()
        self._rows.clear()
        self._keys.clear()

    # ---- 滚动状态 ----
    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """上滚进入浏览模式；滚到底恢复跟随（End 键/滚动条/滚轮均触发）。"""
        if new_value < old_value:
            self._follow = False
        elif new_value >= self.max_scroll_y - 0.5:
            self._follow = True

    def action_follow_end(self) -> None:
        """End 键：恢复自动跟随并滚到底部。"""
        self._follow = True
        self.scroll_end(animate=False)

    # ---- 内部 ----
    def _split(self, text: Text) -> "tuple[str, str, Text]":
        """解析 `[HH:MM:SS] TYPE 内容`；无前缀时按 INFO + 当前时间处理。"""
        m = _PREFIX_RE.match(text.plain)
        if m is not None:
            return m.group(1), m.group(2), _slice_text(text, m.end())
        now = time.strftime("%H:%M:%S")
        return now, "INFO", _slice_text(text, 0)

    def _prune(self) -> None:
        """超过 max_lines 时批量裁剪最旧行（批量阈值随 max_lines 自适应）。"""
        overflow = len(self._keys) - self._max_lines
        threshold = min(_PRUNE_BATCH, max(1, self._max_lines // 2))
        if overflow < threshold:
            return
        drop = max(overflow, threshold)
        for key in self._keys[:drop]:
            try:
                self.remove_row(key)
            except Exception:
                pass
        self._keys = self._keys[drop:]
        self._rows = self._rows[drop:]


__all__ = ["VirtualLog"]
