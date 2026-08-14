"""日志轮转 —— 收敛期 P2（阶段一 1.2）。

单文件按大小轮转（默认 10MB），保留最近 backup_count 份，防止长任务/
长会话下日志文件无限增长。供 CLI / TUI / 传统入口统一复用。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # 单文件 10MB
DEFAULT_BACKUP_COUNT = 7               # 保留最近 7 份（含当前文件）


def make_rotating_file_handler(
    log_path: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    level: int = logging.DEBUG,
    formatter: Optional[logging.Formatter] = None,
) -> RotatingFileHandler:
    """构建按大小轮转的文件 handler；目录不存在时自动创建。

    - max_bytes：单个日志文件超过该字节数即轮转；
    - backup_count：保留的轮转文件份数（旧文件被清理）。
    """
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        str(path), maxBytes=max_bytes, backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    if formatter is not None:
        handler.setFormatter(formatter)
    return handler


__all__ = [
    "make_rotating_file_handler",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_BACKUP_COUNT",
]
