"""异步终端工具 —— 对应设计第 6 节 Terminal。

- 异步子进程，实时读取 stdout/stderr；
- 支持超时终止（terminate -> kill）；
- 支持 output_callback 实时转发每一行（TUI 终端窗格用）；
- Windows 走 PowerShell，Unix 走 /bin/sh，保证跨平台。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List

from agent.tools.base import ExecutionContext, Tool, ToolResult


class TerminalTool(Tool):
    name = "terminal_execute"
    description = "在沙箱工作目录执行 shell 命令并返回输出（支持超时终止）"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "number", "description": "超时秒数，默认 30"},
        },
        "required": ["command"],
    }

    def __init__(self, default_timeout: float = 30.0):
        self.default_timeout = default_timeout

    def _build_argv(self, command: str) -> List[str]:
        if os.name == "nt":
            return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        return ["/bin/sh", "-c", command]

    async def execute(self, params: Dict[str, Any], context: ExecutionContext) -> ToolResult:
        command = str(params.get("command", "")).strip()
        timeout = float(params.get("timeout", self.default_timeout))
        start = time.time()
        callback = context.output_callback

        if not command:
            return ToolResult(success=False, error="命令为空", elapsed_ms=0.0)

        # 沙箱策略在 ToolManager 层已检查；这里在 workspace 下执行
        argv = self._build_argv(command)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=context.workspace,
                env={**os.environ, **context.env},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:  # 进程创建失败
            return ToolResult(success=False, error=f"启动进程失败: {e}",
                              elapsed_ms=(time.time() - start) * 1000)

        async def _read_stream(stream) -> str:
            # 逐行读取；配置了 output_callback 时实时转发，同时收集完整输出
            chunks: List[str] = []
            while True:
                try:
                    line = await stream.readline()
                except (asyncio.LimitOverrunError, ValueError, OSError):
                    break
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                chunks.append(text)
                if callback is not None:
                    callback(text.rstrip("\n"))
            return "".join(chunks)

        try:
            out_task = asyncio.create_task(_read_stream(proc.stdout))
            err_task = asyncio.create_task(_read_stream(proc.stderr))
            await asyncio.wait_for(asyncio.gather(out_task, err_task), timeout=timeout)
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            return ToolResult(
                success=False,
                error=f"命令超时（{timeout}s）并被终止: {command}",
                metadata={"timed_out": True},
                elapsed_ms=(time.time() - start) * 1000,
            )

        out = (await out_task).strip()
        err = (await err_task).strip()
        elapsed = (time.time() - start) * 1000

        if proc.returncode == 0:
            return ToolResult(success=True, output=out or "(empty stdout)",
                              elapsed_ms=elapsed)
        return ToolResult(success=False, output=out, error=err or f"退出码 {proc.returncode}",
                          elapsed_ms=elapsed)
