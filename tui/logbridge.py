"""日志桥 —— 把 Python logging 记录转发到 TUI 主日志区。

Textual 运行期间任何向 stdout 的打印都会与界面交错造成乱码，
因此 TUI 运行期把所有 logging 输出改写到文件，仅把较高级别
（默认 WARNING+）转发为主日志区的一行，既保留可观测性又不破坏屏幕。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from tui.messages import LogMessage


class TuiLogHandler(logging.Handler):
    """把 logging 记录转换为 TUI LogMessage（app 就绪后转发）。"""

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level)
        self._app = None

    def set_app(self, app) -> None:
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        app = self._app
        if app is None:
            return
        try:
            content = record.getMessage()
            if record.name and record.name != "root":
                content = f"{record.name}: {content}"
            app.post_message(LogMessage(record.levelname, content))
        except Exception:
            pass  # 日志转发失败不影响主流程


def install_tui_logging(verbose: bool = False,
                        log_file: Optional[str] = None) -> TuiLogHandler:
    """把 logging 重定向到文件，并挂载转发到 TUI 的 handler。

    移除既有 stdout/console handler，避免日志与 Textual 屏幕交错产生乱码。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler):
            root.removeHandler(handler)

    level = logging.DEBUG if verbose else logging.INFO
    path = Path(log_file or "logs/tui.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    bridge = TuiLogHandler(logging.DEBUG if verbose else logging.WARNING)
    root.addHandler(bridge)
    root.setLevel(level)
    return bridge


__all__ = ["TuiLogHandler", "install_tui_logging"]
