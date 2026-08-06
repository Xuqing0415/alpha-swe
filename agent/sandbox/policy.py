"""沙箱安全策略 —— 路径锚定 + 危险命令拦截（Docker 隔离层预留）。

对应设计第 12 节：FileIO 限定 /workspace、禁止路径穿越；
docker-py 容器、网络隔离、资源限制与快照/回滚在 docker_enabled 时接入。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Tuple

from agent.tools.base import ExecutionContext
from agent.tools.fileio import resolve_workspace_path

logger = logging.getLogger("alpha-swe.sandbox")

TRAVERSAL_PATTERN = re.compile(r"(\.\./|\.\.\\)")

DEFAULT_BLOCKED_COMMANDS = ["sudo", "rm -rf /", "mkfs", "dd if=", ":(){"]
NETWORK_COMMANDS = ["curl", "wget", "pip install", "npm install", "git clone", "git push", "git fetch", "git pull", "ssh", "nc ", "telnet", "ping", "nslookup", "dig "]


class SandboxPolicy:
    def __init__(self, workspace: str = "./workspace",
                 allowed_paths=None, blocked_paths=None, block_commands=None,
                 network_enabled: bool = False, decision_logger=None):
        self.workspace = os.path.abspath(workspace)
        self.allowed_paths = [os.path.abspath(p) for p in (allowed_paths or [])]
        self.blocked_paths = [os.path.abspath(p) for p in (blocked_paths or [])]
        self.blocked_commands = block_commands or DEFAULT_BLOCKED_COMMANDS
        self.network_enabled = network_enabled
        self.decision_logger = decision_logger
        self.violation_count = 0

    def check(self, tool_name: str, params: Dict[str, Any],
              context: ExecutionContext) -> Tuple[bool, str]:
        if tool_name == "file_ops":
            return self._check_file(params, context)
        if tool_name == "terminal_execute":
            return self._check_terminal(params, context)
        return True, ""

    # ---- 文件 ----
    def _check_file(self, params: Dict[str, Any], context: ExecutionContext) -> Tuple[bool, str]:
        path = str(params.get("path", ""))
        if not path:
            return True, ""
        if TRAVERSAL_PATTERN.search(path):
            self._violate(f"路径穿越: {path}")
            return False, f"禁止路径穿越: {path}"
        try:
            target = resolve_workspace_path(context.workspace, path)
        except PermissionError as e:
            self._violate(str(e))
            return False, str(e)
        # 写操作检查黑名单目录
        action = params.get("action", "")
        if action in ("write", "append"):
            for blocked in self.blocked_paths:
                if self._is_under(target, blocked):
                    self._violate(f"写入被禁止目录: {blocked}")
                    return False, f"禁止写入系统目录: {blocked}"
        return True, ""

    # ---- 终端 ----
    def _check_terminal(self, params: Dict[str, Any], context: ExecutionContext) -> Tuple[bool, str]:
        command = str(params.get("command", "")).lower()
        for blocked in self.blocked_commands:
            if blocked.lower() in command:
                self._violate(f"命令含危险关键字: {blocked}")
                return False, f"禁止执行危险命令（包含 {blocked}）"
        if not self.network_enabled:
            for kw in NETWORK_COMMANDS:
                if kw.lower() in command:
                    self._violate(f"网络已禁用: {kw.strip()}")
                    if self.decision_logger is not None:
                        self.decision_logger.record(
                            "block_network_command", "sandbox.network_enabled",
                            False, f"网络已禁用，拦截命令: {command[:80]}",
                        )
                    return False, f"网络已禁用，禁止外部网络命令（包含 {kw.strip()}）"
        return True, ""

    @staticmethod
    def _is_under(path, root) -> bool:
        try:
            return path == os.path.abspath(root) or os.path.abspath(root) in path.parents
        except AttributeError:
            return str(path).startswith(os.path.abspath(root) + os.sep)

    def _violate(self, reason: str) -> None:
        self.violation_count += 1
        logger.warning("沙箱违规 #%d: %s", self.violation_count, reason)

    def stats(self) -> dict:
        return {"violation_count": self.violation_count, "workspace": self.workspace}