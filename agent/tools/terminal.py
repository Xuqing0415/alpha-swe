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


# 只读角色允许的命令白名单（首词匹配，basename）
READ_ONLY_COMMANDS = {
    "cat", "ls", "dir", "grep", "find", "head", "tail", "wc", "file", "diff",
    "stat", "sort", "uniq", "type", "echo", "pwd", "whoami",
    "git",  # 只读子命令在 _read_only_ok 中进一步限制
    "Get-Content", "Get-ChildItem", "Select-String", "Measure-Object",
}
# 只读角色禁止的 shell 元字符（管道/重定向/连接符可能产生写效果）
READ_ONLY_BLOCKED_META = (";", "&&", "||", "|", ">", "<", "2>", "`", "$(")


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

    def __init__(self, default_timeout: float = 30.0, read_only: bool = False):
        self.default_timeout = default_timeout
        self.read_only = read_only

    def _read_only_ok(self, command: str) -> bool:
        """只读模式检查：首词在白名单 + 无写语义元字符 + git 仅允许只读子命令。"""
        cmd = command.strip()
        if not cmd:
            return False
        if any(meta in cmd for meta in READ_ONLY_BLOCKED_META):
            return False
        first = cmd.split()[0].lower()
        base = first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if base not in READ_ONLY_COMMANDS:
            return False
        if base == "git":
            sub = cmd.split()[1].lower() if len(cmd.split()) > 1 else ""
            if sub not in ("status", "diff", "log", "show", "branch", "ls-files"):
                return False
        return True

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
        if self.read_only and not self._read_only_ok(command):
            return ToolResult(
                success=False,
                error=f"只读角色禁止该命令: {command[:80]}",
                elapsed_ms=0.0,
            )

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
