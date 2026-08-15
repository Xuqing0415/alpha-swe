"""后台任务工具 —— 方案 2.4：启动/状态/日志/优雅关闭 长驻进程。

- 环形缓冲区保留最近 1000 行 stdout/stderr；
- 进程意外退出记录退出码与最后 20 行输出；
- 优雅关闭：先 terminate，等待 5 秒，仍存活则强杀整棵进程树。
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from agent.tools.base import ErrorCategory, ExecutionContext, Tool, ToolResult

LOG_LINES = 1000        # 环形缓冲区容量
GRACEFUL_WAIT = 5.0     # 优雅关闭等待秒数
LOG_TAIL = 20           # 崩溃时返回的最后行数


def _decode(data: bytes) -> str:
    """优先 UTF-8，失败按本机编码/GBK 回退，杜绝乱码。"""
    candidates = ["utf-8"]
    try:
        import locale
        enc = locale.getpreferredencoding(False)
        if enc and enc.lower() not in ("utf-8", "utf8"):
            candidates.append(enc)
    except Exception:
        pass
    candidates += ["gbk", "cp936", "big5", "cp1252"]
    for enc in candidates:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


@dataclass
class BackgroundHandle:
    """单个后台任务的状态与输出缓冲。"""
    task_id: str
    command: str
    proc: Any = None
    stdout_buf: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    stderr_buf: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    start_ts: float = field(default_factory=time.time)
    exit_code: Optional[int] = None
    crash_error: Optional[str] = None

    def status(self) -> str:
        if self.proc is not None and self.proc.returncode is None:
            return "running"
        if self.exit_code not in (None, 0):
            return "crashed"
        return "stopped"

    def uptime(self) -> float:
        return time.time() - self.start_ts


class BackgroundTaskManager:
    """管理所有后台任务：启动、状态、日志、关闭、会话清理。"""

    def __init__(self) -> None:
        self._tasks: Dict[str, BackgroundHandle] = {}

    # ---- 生命周期 ----
    async def start(self, command: str, workspace: str) -> BackgroundHandle:
        """启动长驻进程；进程意外退出时记录退出码与最后输出。"""
        argv = self._build_argv(command)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workspace,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        handle = BackgroundHandle(
            task_id=uuid.uuid4().hex[:8], command=command, proc=proc,
        )
        self._tasks[handle.task_id] = handle
        handle.reader_tasks = [
            asyncio.create_task(self._read(proc.stdout, handle.stdout_buf)),
            asyncio.create_task(self._read(proc.stderr, handle.stderr_buf)),
            asyncio.create_task(self._watch_exit(handle)),
        ]
        return handle

    async def stop(self, task_id: str, graceful: bool = True) -> str:
        """优雅关闭：terminate -> 等 GRACEFUL_WAIT -> 强杀进程树。"""
        handle = self._tasks.get(task_id)
        if handle is None:
            return f"未找到后台任务: {task_id}"
        proc = handle.proc
        if proc is None or proc.returncode is not None:
            return f"任务已停止（exit_code={proc.returncode if proc is not None else 'unknown'}）"
        if graceful:
            if os.name == "nt":
                # Windows：先对整棵进程树发终止信号（避免 wrapper 退出后子进程成孤儿）
                await self._tree_taskkill(proc.pid, force=False)
            else:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=GRACEFUL_WAIT)
            except asyncio.TimeoutError:
                pass
        if proc.returncode is None:
            await self._kill_tree(proc)
            await asyncio.wait_for(proc.wait(), timeout=10)
        # 用户主动停止的进程视为正常退出（非崩溃）
        handle.exit_code = 0
        handle.crash_error = None
        return f"已停止 {task_id}（exit_code=0）"

    async def shutdown_all(self, graceful: bool = False) -> None:
        """会话结束时清理所有后台任务。"""
        for task_id in list(self._tasks.keys()):
            try:
                await self.stop(task_id, graceful=graceful)
            except Exception:
                pass
        self._tasks.clear()

    # ---- 查询 ----
    def list_tasks(self) -> List[str]:
        return list(self._tasks.keys())

    def status(self, task_id: str) -> Optional[BackgroundHandle]:
        return self._tasks.get(task_id)

    def tail_logs(self, task_id: str, lines: int = 50) -> str:
        """合并 stdout/stderr 环形缓冲，返回最近 N 行。"""
        handle = self._tasks.get(task_id)
        if handle is None:
            return f"未找到后台任务: {task_id}"
        combined = list(handle.stdout_buf) + list(handle.stderr_buf)
        body = "\n".join(combined[-max(1, int(lines)):])
        return body or "（暂无输出）"

    # ---- 内部 ----
    @staticmethod
    def _build_argv(command: str) -> List[str]:
        if os.name == "nt":
            prefix = (
                "$OutputEncoding=[System.Text.Encoding]::UTF8;"
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                "chcp 65001 | Out-Null; "
            )
            return ["powershell", "-NoProfile", "-NonInteractive",
                    "-Command", prefix + command + "; exit $LASTEXITCODE"]
        return ["/bin/sh", "-c", command]

    async def _read(self, stream, buf: Deque[str]) -> None:
        while True:
            try:
                line = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError, OSError):
                break
            if not line:
                break
            text = _decode(line).rstrip("\n")
            if text:
                buf.append(text)

    async def _watch_exit(self, handle: BackgroundHandle) -> None:
        code = await handle.proc.wait()
        handle.exit_code = code
        if code not in (None, 0):
            tail = "\n".join(
                list(handle.stderr_buf)[-LOG_TAIL:] or
                list(handle.stdout_buf)[-LOG_TAIL:]
            )
            handle.crash_error = (
                f"后台任务意外退出（exit_code={code}）\n最后输出:\n{tail}"
            )

    async def _kill_tree(self, proc) -> None:
        """Windows 用 taskkill /T /F 强杀整棵进程树，Unix 用 SIGKILL。"""
        if os.name == "nt":
            await self._tree_taskkill(proc.pid, force=True)
        else:
            proc.kill()

    async def _tree_taskkill(self, pid: int, force: bool) -> None:
        """Windows 树级终止：force=True 加 /F，否则普通终止。"""
        args = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            args.append("/F")
        try:
            killer = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except Exception:
            pass


class BackgroundTaskTool(Tool):
    """后台任务管理工具：start / status / logs / stop。"""
    name = "background_task"
    description = ("后台任务管理: start 启动长驻进程（如开发服务器）并返回 task_id；"
                   "status 查询运行状态；logs 查看最近 N 行输出；stop 优雅关闭。"
                   "后台任务不设超时，可跨轮次存活。")
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "status", "logs", "stop"],
            },
            "command": {"type": "string",
                        "description": "要启动的后台命令（action=start）"},
            "task_id": {"type": "string", "description": "后台任务 ID"},
            "lines": {"type": "integer", "description": "返回最近 N 行日志，默认 50"},
            "graceful": {"type": "boolean",
                         "description": "先优雅终止，5 秒后强杀（默认 true）"},
        },
        "required": ["action"],
    }

    def __init__(self, manager: Optional[BackgroundTaskManager] = None,
                 decision_logger=None):
        self.manager = manager or BackgroundTaskManager()
        self.decision_logger = decision_logger

    def _log(self, name: str, config_key: str, config_value: Any,
             decision: str) -> None:
        if self.decision_logger is not None:
            try:
                self.decision_logger.record(name, config_key, config_value,
                                            decision)
            except Exception:
                pass

    async def execute(self, params: Dict[str, Any],
                      context: ExecutionContext) -> ToolResult:
        action = str(params.get("action", ""))
        start = time.time()

        if action == "start":
            command = str(params.get("command", "")).strip()
            if not command:
                return ToolResult(
                    success=False, error="启动后台任务需要 command 参数",
                    elapsed_ms=(time.time() - start) * 1000,
                    error_category=ErrorCategory.PERMANENT,
                )
            try:
                handle = await self.manager.start(command, context.workspace)
            except Exception as e:
                # 方案 2.4：启动失败也写入决策日志，保证可观测性
                self._log("background.start_failed", "tools.background_task",
                  command,
                          f"后台任务启动失败: {e}")
                return ToolResult(
                    success=False, error=f"后台任务启动失败: {e}",
                    elapsed_ms=(time.time() - start) * 1000,
                    error_category=ErrorCategory.TRANSIENT,
                )
            self._log("background.start", "tools.background_task", command,
                      f"后台任务 {handle.task_id} 启动: {command[:100]}")
            return ToolResult(
                success=True,
                output=(f"后台任务已启动: {handle.task_id}\n"
                        f"命令: {command}\n"
                        f"可用 background_task status/logs/stop 管理"),
                metadata={"task_id": handle.task_id, "command": command},
                elapsed_ms=(time.time() - start) * 1000,
            )

        if action == "status":
            task_id = str(params.get("task_id", ""))
            handle = self.manager.status(task_id)
            if handle is None:
                return ToolResult(
                    success=False,
                    error=f"未找到后台任务: {task_id}（当前: {self.manager.list_tasks()}）",
                    elapsed_ms=(time.time() - start) * 1000,
                    error_category=ErrorCategory.PERMANENT,
                )
            st = handle.status()
            lines = [
                f"任务: {handle.task_id}  状态: {st}",
                f"命令: {handle.command[:120]}",
                f"运行时长: {handle.uptime():.1f}s",
            ]
            if handle.exit_code is not None:
                lines.append(f"退出码: {handle.exit_code}")
            if handle.crash_error:
                lines.append(handle.crash_error)
            self._log("background.status", "tools.background_task", st,
                      f"后台任务 {task_id} 状态: {st}")
            return ToolResult(
                success=True, output="\n".join(lines),
                metadata={"task_id": task_id, "status": st,
                          "uptime": round(handle.uptime(), 1),
                          "exit_code": handle.exit_code},
                elapsed_ms=(time.time() - start) * 1000,
            )

        if action == "logs":
            task_id = str(params.get("task_id", ""))
            lines_n = int(params.get("lines", 50) or 50)
            body = self.manager.tail_logs(task_id, lines_n)
            if body.startswith("未找到"):
                return ToolResult(
                    success=False, error=body,
                    elapsed_ms=(time.time() - start) * 1000,
                    error_category=ErrorCategory.PERMANENT,
                )
            return ToolResult(
                success=True,
                output=f"[后台 {task_id} 最近 {lines_n} 行]\n{body}",
                metadata={"task_id": task_id, "lines": lines_n},
                elapsed_ms=(time.time() - start) * 1000,
            )

        if action == "stop":
            task_id = str(params.get("task_id", ""))
            graceful = bool(params.get("graceful", True))
            msg = await self.manager.stop(task_id, graceful=graceful)
            handle = self.manager.status(task_id)
            final_status = handle.status() if handle else "unknown"
            self._log("background.stop", "tools.background_task", graceful,
                      f"后台任务 {task_id} 关闭完成（graceful={graceful}）")
            return ToolResult(
                success=True, output=msg,
                metadata={"task_id": task_id, "graceful": graceful,
                          "status": final_status},
                elapsed_ms=(time.time() - start) * 1000,
            )

        return ToolResult(
            success=False, error=f"未知操作: {action}",
            elapsed_ms=(time.time() - start) * 1000,
            error_category=ErrorCategory.PERMANENT,
        )