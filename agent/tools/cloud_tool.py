# -*- coding: utf-8 -*-
"""云/DevOps CLI 封装（方向二阶段三 3.2）。

- 封装 aws / kubectl / docker 等 CLI，参数透传；
- 默认禁用：仅在配置 ``tools.cloud.enabled=true`` 时注册；
- 每次执行需要显式 ``confirm=true``，命令进入决策日志；
- 危险命令黑名单（delete/destroy/force/--yes 等）默认拦截。
"""
from __future__ import annotations

import asyncio
import shlex
from typing import Any, Dict

from agent.tools.base import (ErrorCategory, ExecutionContext, Tool,
                              ToolResult)

_ALLOWED_TOOLS = ("aws", "kubectl", "docker", "gcloud", "az")
_BLOCKED_WORDS = ("delete", "destroy", "rm -", "force", "--yes", "-y ",
                  "drop", "truncate", "shutdown", "reboot")


class CloudTool(Tool):
    name = "cloud"
    description = ("执行云/DevOps CLI（aws/kubectl/docker/gcloud/az）。"
                   "需 confirm=true 确认；危险子命令默认拦截。")
    parameters = {
        "type": "object",
        "properties": {
            "tool": {"type": "string",
                     "enum": list(_ALLOWED_TOOLS),
                     "description": "CLI 名称"},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "参数列表（不要用 shell 拼接）"},
            "confirm": {"type": "boolean", "default": False},
            "timeout": {"type": "number", "default": 60},
        },
        "required": ["tool", "args", "confirm"],
    }

    def __init__(self, default_timeout: float = 60.0, decision_logger=None):
        self.default_timeout = max(5.0, default_timeout)
        self.decision_logger = decision_logger

    async def execute(self, params: Dict[str, Any],
                      context: ExecutionContext) -> ToolResult:
        tool = str(params.get("tool") or "")
        args = [str(a) for a in (params.get("args") or [])]
        timeout = float(params.get("timeout") or self.default_timeout)
        if tool not in _ALLOWED_TOOLS:
            return ToolResult(success=False,
                              error=f"不支持的 CLI: {tool}",
                              error_category=ErrorCategory.PERMANENT)
        if params.get("confirm") is not True:
            return ToolResult(
                success=False,
                error="云操作需要 confirm=true 显式确认",
                error_category=ErrorCategory.PERMISSION)
        joined = " ".join(shlex.quote(a) for a in args)
        lowered = (" ".join(args)).lower()
        for bad in _BLOCKED_WORDS:
            if bad in lowered:
                return ToolResult(
                    success=False,
                    error=f"危险子命令被拦截: {bad}",
                    error_category=ErrorCategory.PERMISSION)
        if self.decision_logger is not None:
            self.decision_logger.record(
                "cloud.exec", "tools.cloud", True,
                f"{tool} {joined[:120]}",
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                tool, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                raw = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.communicate()
                except Exception:
                    pass
                return ToolResult(
                    success=False, error=f"命令超时（{timeout:.0f}s）",
                    error_category=ErrorCategory.TRANSIENT)
        except FileNotFoundError:
            return ToolResult(
                success=False, error=f"未找到命令: {tool}",
                error_category=ErrorCategory.CONFIGURATION)
        except Exception as e:
            return ToolResult(success=False, error=f"启动失败: {e}",
                              error_category=ErrorCategory.TRANSIENT)
        out = (raw[0].decode("utf-8", errors="replace") if raw else "")
        return ToolResult(success=proc.returncode == 0,
                          output=out[:4000] or f"退出码 {proc.returncode}",
                          metadata={"returncode": proc.returncode})
