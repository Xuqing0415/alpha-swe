"""日志桥 —— 把 Python logging 记录转发到 TUI 主日志区。

Textual 运行期间任何向 stdout 的打印都会与界面交错造成乱码，
因此 TUI 运行期把所有 logging 输出改写到文件，仅把较高等级
（默认 INFO+）转发为主日志区的一行，既保留可观测性又不破坏屏幕。
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Optional

from tui.messages import LogMessage

# 会话级 trace id（进程内固定，8 位 hex），随每条日志落盘，便于跨文件关联
_SESSION_ID = secrets.token_hex(4)

# 日志级别过滤循环（Ctrl+L 切换，方案 4.2 噪声控制）
LEVEL_CYCLE = [logging.INFO, logging.WARNING, logging.DEBUG, logging.CRITICAL]
_LEVEL_NAMES = {logging.DEBUG: "DEBUG", logging.INFO: "INFO",
                logging.WARNING: "WARN", logging.ERROR: "ERROR",
                logging.CRITICAL: "CRITICAL"}


class StdLogFormatter(logging.Formatter):
    """标准日志格式：[时间戳] [级别] [模块] [session_id] 内容。"""

    def format(self, record: logging.LogRecord) -> str:
        record.session = _SESSION_ID
        return super().format(record)


class TuiLogHandler(logging.Handler):
    """把 logging 记录转换为 TUI LogMessage（app 就绪后转发）。"""

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._app = None

    def set_app(self, app) -> None:
        self._app = app

    def cycle_level(self) -> int:
        """循环切换转发级别：INFO -> WARN -> DEBUG -> CRITICAL -> INFO。"""
        idx = LEVEL_CYCLE.index(self.level) if self.level in LEVEL_CYCLE else 0
        nxt = LEVEL_CYCLE[(idx + 1) % len(LEVEL_CYCLE)]
        self.setLevel(nxt)
        return nxt

    def level_label(self) -> str:
        return _LEVEL_NAMES.get(self.level, str(self.level))

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
                        log_file: Optional[str] = None,
                        json_log_dir: Optional[str] = None) -> TuiLogHandler:
    """把 logging 重定向到文件，并挂载转发到 TUI 的 handler。

    移除既有 stdout/console handler，避免日志与 Textual 屏幕交错产生乱码；
    文件始终保存全量日志（含 DEBUG），TUI 默认只转发 INFO+（可 Ctrl+L 切换）；
    json_log_dir 非空时同时挂载结构化 JSONL handler（第 10 节）。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler):
            root.removeHandler(handler)

    path = Path(log_file or "logs/tui.log")
    # 收敛期 P2：文件按大小轮转（10MB），保留最近 7 份，防止无限增长
    from agent.observability.logging_setup import make_rotating_file_handler
    file_handler = make_rotating_file_handler(
        str(path),
        formatter=StdLogFormatter(
            "%(asctime)s [%(levelname)s] [%(name)s] [%(session)s] %(message)s"
        ),
    )
    root.addHandler(file_handler)

    bridge_level = logging.DEBUG if verbose else logging.INFO
    bridge = TuiLogHandler(bridge_level)
    root.addHandler(bridge)
    # 根级别恒为 DEBUG：文件 handler 全量落盘，界面过滤交给桥的级别
    root.setLevel(logging.DEBUG)
    if json_log_dir:
        try:
            from agent.observability.otel import JsonLinesLogHandler
            root.addHandler(JsonLinesLogHandler(
                json_log_dir, session_id=_SESSION_ID))
        except Exception:
            logger.warning("结构化 JSONL 日志初始化失败")
    return bridge


__all__ = ["TuiLogHandler", "install_tui_logging"]