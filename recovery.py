"""错误恢复机制：重试3次 + fallback策略 + 全局异常捕获 + 崩溃自动重启"""
import time
import logging
import traceback
from typing import Callable, Optional, Any
from functools import wraps
from tools.base import ToolResult

logger = logging.getLogger("alpha-swe.recovery")

# 工具 fallback 策略映射
FALLBACK_STRATEGIES = {
    "terminal_execute": {
        "grep": "find . -type f -name '*.py' | head -20",
        "find": "dir /s /b" if __import__('os').name == 'nt' else "ls -la",
        "pip install": "pip install --user",
    },
    "file_ops": {
        "read": "terminal_execute",  # 读失败 -> 改用 cat
    }
}


class RetryConfig:
    """重试配置"""
    def __init__(self, max_retries: int = 3, delay: float = 1.0,
                 backoff: float = 2.0, max_delay: float = 30.0):
        self.max_retries = max_retries
        self.delay = delay
        self.backoff = backoff
        self.max_delay = max_delay


class ErrorRecovery:
    """错误恢复管理器"""

    def __init__(self, config: RetryConfig = None):
        self.config = config or RetryConfig()
        self.retry_count = 0
        self.fallback_count = 0
        self.crash_count = 0
        self.error_log: list = []

    def execute_with_retry(self, fn: Callable, *args, **kwargs) -> Any:
        """带重试的执行器"""
        last_error = None
        delay = self.config.delay

        for attempt in range(self.config.max_retries + 1):
            try:
                result = fn(*args, **kwargs)
                # 检查 ToolResult 类型
                if isinstance(result, ToolResult) and not result.success:
                    last_error = result.error
                    if attempt < self.config.max_retries:
                        logger.warning(
                            f"重试 {attempt + 1}/{self.config.max_retries}: "
                            f"工具执行失败: {last_error}"
                        )
                        self.retry_count += 1
                        time.sleep(delay)
                        delay = min(delay * self.config.backoff, self.config.max_delay)
                        continue
                    return result
                return result
            except Exception as e:
                last_error = str(e)
                if attempt < self.config.max_retries:
                    logger.warning(
                        f"重试 {attempt + 1}/{self.config.max_retries}: {e}"
                    )
                    self.retry_count += 1
                    time.sleep(delay)
                    delay = min(delay * self.config.backoff, self.config.max_delay)
                else:
                    logger.error(f"达到最大重试次数: {e}")
                    raise

        return ToolResult(success=False, output="", error=f"重试耗尽: {last_error}")

    def apply_fallback(self, tool_name: str, params: dict, error: str) -> Optional[dict]:
        """根据错误类型应用 fallback 策略"""
        strategies = FALLBACK_STRATEGIES.get(tool_name, {})
        if not strategies:
            return None

        if tool_name == "terminal_execute":
            cmd = params.get("command", "")
            for keyword, fallback_cmd in strategies.items():
                if keyword in cmd.lower():
                    if isinstance(fallback_cmd, str):
                        logger.info(f"Fallback: {keyword} -> {fallback_cmd}")
                        self.fallback_count += 1
                        return {"action": "terminal_execute",
                                "params": {"command": fallback_cmd}}

        if tool_name == "file_ops":
            # 文件不存在 -> 改用终端搜索
            if "不存在" in str(error) or "No such file" in str(error):
                path = params.get("path", "")
                if path:
                    import os
                    filename = os.path.basename(path)
                    logger.info(f"Fallback: 文件不存在 -> 搜索 {filename}")
                    self.fallback_count += 1
                    return {"action": "terminal_execute",
                            "params": {"command": f"find . -name '{filename}' -type f 2>nul"}}

        return None

    def crash_handler(self, loop_instance, max_restarts: int = 3):
        """全局崩溃处理装饰器"""
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                restarts = 0
                while restarts <= max_restarts:
                    try:
                        return fn(*args, **kwargs)
                    except Exception as e:
                        self.crash_count += 1
                        self.error_log.append({
                            "error": str(e),
                            "traceback": traceback.format_exc(),
                            "crash_count": self.crash_count
                        })
                        logger.error(f"Loop 崩溃 #{self.crash_count}: {e}")
                        logger.error(traceback.format_exc())

                        restarts += 1
                        if restarts <= max_restarts:
                            logger.info(f"自动重启 Loop (attempt {restarts}/{max_restarts})...")
                            time.sleep(2)
                            # 重新初始化状态
                            if loop_instance:
                                loop_instance.state.status = "idle"
                        else:
                            logger.critical("达到最大重启次数，放弃")
                            raise
                return None
            return wrapper
        return decorator

    def get_stats(self) -> dict:
        return {
            "retry_count": self.retry_count,
            "fallback_count": self.fallback_count,
            "crash_count": self.crash_count,
            "recent_errors": self.error_log[-5:]
        }