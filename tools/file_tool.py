"""文件读写工具"""
import os
import time
from .base import BaseTool, ToolResult


class FileTool(BaseTool):
    name = "file_ops"
    description = "文件读写操作：read/write/append"

    def execute(self, action: str, path: str, content: str = "", **kwargs) -> ToolResult:
        start = time.time()
        try:
            if action == "read":
                if not os.path.exists(path):
                    return ToolResult(
                        success=False, output="", error=f"文件不存在: {path}",
                        elapsed_ms=(time.time() - start) * 1000
                    )
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    data = f.read()
                return ToolResult(
                    success=True, output=data,
                    metadata={"size": len(data)},
                    elapsed_ms=(time.time() - start) * 1000
                )

            elif action == "write":
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                return ToolResult(
                    success=True, output=f"写入成功: {path}",
                    metadata={"size": len(content)},
                    elapsed_ms=(time.time() - start) * 1000
                )

            elif action == "append":
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(content)
                return ToolResult(
                    success=True, output=f"追加成功: {path}",
                    elapsed_ms=(time.time() - start) * 1000
                )

            else:
                return ToolResult(
                    success=False, output="", error=f"未知操作: {action}",
                    elapsed_ms=(time.time() - start) * 1000
                )
        except Exception as e:
            return ToolResult(
                success=False, output="", error=str(e),
                elapsed_ms=(time.time() - start) * 1000
            )