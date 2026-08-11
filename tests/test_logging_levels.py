"""日志分级与噪声控制测试（方案 4.2）：标准格式、全量落盘、级别循环。"""
import logging
import re

import pytest

from tui.logbridge import (LEVEL_CYCLE, StdLogFormatter, TuiLogHandler,
                           install_tui_logging)


def test_std_log_formatter_includes_session_and_level(ws_tmp):
    """标准格式：[时间戳] [级别] [模块] [session_id] 内容。"""
    fmt = StdLogFormatter(
        "%(asctime)s [%(levelname)s] [%(name)s] [%(session)s] %(message)s"
    )
    record = logging.LogRecord(
        name="alpha-swe.tools", level=logging.INFO, pathname=__file__,
        lineno=1, msg="hello log", args=(), exc_info=None,
    )
    line = fmt.format(record)
    assert "[INFO]" in line
    assert "[alpha-swe.tools]" in line
    m = re.search(r"\[([0-9a-f]{8})\] hello log", line)
    assert m, line


def test_handler_cycle_level_rotates():
    h = TuiLogHandler(logging.INFO)
    assert h.level == logging.INFO
    assert h.cycle_level() == logging.WARNING
    assert h.cycle_level() == logging.DEBUG
    assert h.cycle_level() == logging.CRITICAL
    assert h.cycle_level() == logging.INFO  # 回到起点
    assert h.level_label() == "INFO"


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def test_install_tui_logging_writes_full_logs(ws_tmp, restore_root_logging):
    """非 verbose 模式下文件仍保存 DEBUG 全量日志，桥默认 INFO。"""
    log_file = str(ws_tmp / "tui.log")
    bridge = install_tui_logging(verbose=False, log_file=log_file)
    assert bridge.level == logging.INFO
    logging.getLogger("alpha-swe.test.p4").debug("debug-line-保留")
    logging.getLogger("alpha-swe.test.p4").info("info-line")
    # 关闭 file handler 让记录落盘
    for h in list(logging.getLogger().handlers):
        if isinstance(h, logging.FileHandler):
            h.close()
            logging.getLogger().removeHandler(h)
    content = open(log_file, encoding="utf-8").read()
    assert "debug-line-保留" in content
    assert "info-line" in content
    assert "[DEBUG]" in content and "[alpha-swe.test.p4]" in content


def test_tui_binding_ctrl_l_cycles_log_level():
    """Ctrl+L 绑定到日志级别过滤 action。"""
    from tui.app import AlphaSWEApp
    keys = {b.key: b.action for b in AlphaSWEApp.BINDINGS}
    assert keys.get("ctrl+l") == "cycle_log_level"
    assert keys.get("ctrl+u") == "clear_terminal"


def test_level_cycle_matches_design_order():
    assert LEVEL_CYCLE == [logging.INFO, logging.WARNING,
                           logging.DEBUG, logging.CRITICAL]