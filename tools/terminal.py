"""终端命令执行工具"""
import subprocess
import time
from .base import BaseTool, ToolResult


class TerminalTool(BaseTool):
    name = "terminal_execute"
    description = "在终端执行 shell 命令并返回结果"

    def execute(self, command: str, timeout: int = 30, **kwargs) -> ToolResult:
        start = time.time()
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=kwargs.get("cwd")
            )
            elapsed = (time.time() - start) * 1000
            if result.returncode == 0:
                return ToolResult(
                    success=True,
                    output=result.stdout.strip() or "(empty stdout)",
                    elapsed_ms=elapsed
                )
            else:
                return ToolResult(
                    success=False,
                    output=result.stdout.strip(),
                    error=result.stderr.strip(),
                    elapsed_ms=elapsed
                )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error=f"命令超时 ({timeout}s)",
                elapsed_ms=(time.time() - start) * 1000
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=str(e),
                elapsed_ms=(time.time() - start) * 1000
            )