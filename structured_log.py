"""结构化日志：JSON Lines 格式 + trace_id 链路追踪"""
import json
import logging
import uuid
import threading
import os
from datetime import datetime


class TraceContext(threading.local):
    """线程本地 trace 上下文"""
    trace_id: str = ""
    span_id: str = ""


_trace_ctx = TraceContext()


def new_trace() -> str:
    """生成新的 trace_id"""
    tid = str(uuid.uuid4())[:12]
    _trace_ctx.trace_id = tid
    _trace_ctx.span_id = str(uuid.uuid4())[:8]
    return tid


def get_trace_id() -> str:
    return _trace_ctx.trace_id or "no-trace"


def get_span_id() -> str:
    return _trace_ctx.span_id or "no-span"


class JSONLinesHandler(logging.Handler):
    """JSON Lines 格式日志处理器（常驻文件句柄，避免每条日志都打开/关闭文件）"""

    def __init__(self, log_dir: str = "./logs", filename: str = None):
        super().__init__()
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, filename or f"structured_{timestamp}.jsonl")
        self._lock = threading.Lock()
        self._stream = open(self.log_file, "a", encoding="utf-8")

    def emit(self, record: logging.LogRecord):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": get_trace_id(),
            "span_id": get_span_id(),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])

        with self._lock:
            self._stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._stream.flush()

    def close(self):
        stream = getattr(self, "_stream", None)
        if stream is not None:
            with self._lock:
                stream.close()
        super().close()


def setup_structured_logging(log_dir: str = "./logs", console: bool = True):
    """配置结构化日志（幂等：重复调用不会叠加 handler）"""
    global _configured_log_file
    root = logging.getLogger()
    if any(isinstance(h, JSONLinesHandler) for h in root.handlers):
        return _configured_log_file or ""
    root.setLevel(logging.DEBUG)

    # JSON Lines 文件处理器
    json_handler = JSONLinesHandler(log_dir=log_dir)
    json_handler.setLevel(logging.DEBUG)
    root.addHandler(json_handler)

    # 控制台处理器
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            )
        )
        root.addHandler(console_handler)

    # 生成初始 trace
    new_trace()

    _configured_log_file = json_handler.log_file
    return json_handler.log_file


_configured_log_file: str = ""