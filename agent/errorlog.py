"""统一错误出口 —— CLI/TUI 最外层异常捕获、上下文摘要与全量落盘。

对应「命令行/TUI 驱动新核心系统性排查」第一步 1.1：
- 捕获后输出完整 traceback（不只 str(e)）；
- 附带当前上下文摘要（任务/工具/模块/trace_id 等由调用方提供）；
- 全量错误写入 logs/cli_error_YYYYMMDD_HHMMSS.log，TUI 显示空间有限时
  文件里仍有完整信息。

所有写入函数自身绝不抛异常（失败只降级为日志告警），避免二次崩溃。
"""
from __future__ import annotations

import logging
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("alpha-swe.errorlog")


def write_error_log(
    exc: BaseException,
    *,
    context: Optional[Dict[str, Any]] = None,
    log_dir: Optional[str] = None,
    session_id: str = "",
) -> str:
    """把异常全量 traceback 与上下文摘要写入 logs/cli_error_*.log，返回路径。

    写入失败时降级：记录 WARN 并返回空串（调用方仍可打印到 stderr）。
    """
    try:
        dir_path = Path(log_dir) if log_dir else Path.cwd() / "logs"
        dir_path.mkdir(parents=True, exist_ok=True)
        name = time.strftime("cli_error_%Y%m%d_%H%M%S")
        if session_id:
            name += "_" + session_id
        path = dir_path / (name + ".log")
        tb = "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))
        lines = [
            "time: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "exception: %s: %s"
            % (type(exc).__module__ + "." + type(exc).__name__, exc),
        ]
        if context:
            lines.append("context:")
            for key, value in context.items():
                lines.append("  %s: %s" % (key, value))
        lines.append("traceback:")
        lines.append(tb.rstrip("\n"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)
    except Exception as e:  # 错误日志自身失败不能影响主流程
        logger.error("写入错误日志失败: %s", e)
        return ""


def print_error(
    exc: BaseException,
    *,
    context: Optional[Dict[str, Any]] = None,
    log_path: str = "",
) -> None:
    """向 stderr 输出可读错误摘要 + 上下文 + 完整 traceback。"""
    out = sys.stderr
    out.write("\n[致命错误] %s: %s\n" % (type(exc).__name__, exc))
    if context:
        for key, value in context.items():
            out.write("  %s: %s\n" % (key, value))
    if log_path:
        out.write("  完整错误已写入: %s\n" % log_path)
    out.write("".join(traceback.format_exception(
        type(exc), exc, exc.__traceback__)))
    out.flush()


def guard(exit_code: int = 1, context_fn=None):
    """装饰器：统一捕获函数体内异常，落盘 + stderr 打印后返回退出码。

    context_fn 可选：调用时接收异常对象，返回上下文 dict。
    """
    def deco(fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                ctx = context_fn(e) if context_fn else None
                path = write_error_log(e, context=ctx)
                print_error(e, context=ctx, log_path=path)
                return exit_code
        return wrapper
    return deco