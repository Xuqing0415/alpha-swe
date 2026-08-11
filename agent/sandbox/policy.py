"""沙箱安全策略 —— 路径锚定 + 危险命令拦截 + 网络细粒度策略 + 文件保护。

对应设计第 12、5 节：
- FileIO 限定 /workspace、禁止路径穿越（含 NUL/UNC 检查）；
- 危险命令拦截（sudo / rm -rf / 等）；
- 网络策略 deny | allowlist | allow（allowlist 放行 network_allowed_commands）；
- 网络请求审计：提取 curl/wget/git clone 目标 URL 记录决策日志；
- 假网络模式：curl/wget 返回预设响应，不产生真实请求；
- 受保护路径防删：rm/del/Remove-Item 命中 protected_paths 时拦截。
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from agent.tools.base import ExecutionContext
from agent.tools.fileio import resolve_workspace_path

logger = logging.getLogger("alpha-swe.sandbox")

TRAVERSAL_PATTERN = re.compile(r"(\.\./|\.\.\\)")
URL_PATTERN = re.compile(r"(?:https?://|git@|ssh://)[^\s\"'<>]+")

DEFAULT_BLOCKED_COMMANDS = ["sudo", "rm -rf /", "mkfs", "dd if=", ":(){"]
NETWORK_COMMANDS = ["curl", "wget", "pip install", "pip3 install", "pip download",
                    "npm install", "pnpm install", "yarn add", "apt-get", "apt ",
                    "git clone", "git push", "git fetch", "git pull", "ssh",
                    "nc ", "telnet", "ping", "nslookup", "dig "]
# 删除命令：提取目标路径做受保护检查
DELETE_PATTERNS = [
    re.compile(r"\brm\s+(-[a-z]*r[a-z]*\s+)?(.*)", re.IGNORECASE),
    re.compile(r"\bRemove-Item\b(.*)", re.IGNORECASE),
    re.compile(r"\bdel\s+(.*)", re.IGNORECASE),
    re.compile(r"\brmdir\s+(.*)", re.IGNORECASE),
]


class SandboxPolicy:
    def __init__(self, workspace: str = "./workspace",
                 allowed_paths=None, blocked_paths=None, block_commands=None,
                 network_enabled: bool = False, decision_logger=None,
                 network_policy: str = "deny",
                 network_allowed_commands: Optional[List[str]] = None,
                 fake_network: bool = False,
                 fake_network_responses: Optional[Dict[str, str]] = None,
                 protected_paths: Optional[List[str]] = None):
        self.workspace = os.path.abspath(workspace)
        self.allowed_paths = [os.path.abspath(p) for p in (allowed_paths or [])]
        self.blocked_paths = [os.path.abspath(p) for p in (blocked_paths or [])]
        self.blocked_commands = block_commands or DEFAULT_BLOCKED_COMMANDS
        self.network_enabled = network_enabled
        self.network_policy = network_policy or "deny"
        self.network_allowed_commands = list(network_allowed_commands or [])
        self.fake_network = fake_network
        self.fake_network_responses = dict(fake_network_responses or {})
        self.protected_paths = list(protected_paths or [])
        self.decision_logger = decision_logger
        self.violation_count = 0

    # ---- 入口 ----
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
        if "\x00" in path:
            self._violate(f"路径含 NUL 字节: {path}")
            return False, "禁止包含 NUL 字节的路径"
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
        if action in ("write", "append", "edit"):
            for blocked in self.blocked_paths:
                if self._is_under(target, blocked):
                    self._violate(f"写入被禁止目录: {blocked}")
                    return False, f"禁止写入系统目录: {blocked}"
            if self._is_protected(path):
                self._violate(f"写入受保护路径: {path}")
                return False, f"禁止修改受保护文件: {path}"
        return True, ""

    # ---- 终端 ----
    def _check_terminal(self, params: Dict[str, Any], context: ExecutionContext) -> Tuple[bool, str]:
        command = str(params.get("command", ""))
        lowered = command.lower()
        for blocked in self.blocked_commands:
            if blocked.lower() in lowered:
                self._violate(f"命令含危险关键字: {blocked}")
                return False, f"禁止执行危险命令（包含 {blocked}）"
        # 受保护路径防删（rm / del / Remove-Item）
        for target in self._deleted_paths(command):
            if self._is_protected(target):
                self._violate(f"删除受保护路径: {target}")
                if self.decision_logger is not None:
                    self.decision_logger.record(
                        "file.protect", "sandbox.protected_paths", target,
                        f"拦截删除受保护路径: {command[:80]}",
                    )
                return False, f"禁止删除受保护路径: {target}"
        # 网络请求审计：任何网络命令都记录目标 URL（无论是否拦截）
        self._audit_network(command)
        # 网络策略
        if not self.network_enabled and self._is_network_command(lowered):
            if self.network_policy == "deny" or not self._allowed_network(lowered):
                self._violate(f"网络策略拦截: {command}")
                if self.decision_logger is not None:
                    self.decision_logger.record(
                        "block_network_command", "sandbox.network_policy",
                        self.network_policy,
                        f"网络策略 {self.network_policy} 拦截命令: {command[:80]}",
                    )
                return False, (
                    f"网络已禁用（策略 {self.network_policy}），"
                    f"禁止外部网络命令: {command[:60]}"
                )
        return True, ""

    # ---- 假网络 ----
    def intercept(self, tool_name: str, params: Dict[str, Any]) -> Optional[str]:
        """假网络拦截：curl/wget 命中预设响应时返回响应文本，否则 None。"""
        if not self.fake_network or tool_name != "terminal_execute":
            return None
        command = str(params.get("command", "")).strip()
        url = self._first_url(command)
        if not url:
            return None
        for prefix, body in sorted(self.fake_network_responses.items(), key=lambda kv: -len(kv[0])):
            if url.startswith(prefix):
                if self.decision_logger is not None:
                    self.decision_logger.record(
                        "network.fake", "sandbox.fake_network", True,
                        f"假网络命中 {prefix}，返回预设响应（未发起真实请求）: {url[:80]}",
                    )
                return f"(fake-network) {url}\n{body}"
        return None

    # ---- 内部 ----
    def _is_network_command(self, lowered: str) -> bool:
        return any(kw in lowered for kw in NETWORK_COMMANDS)

    def _allowed_network(self, lowered: str) -> bool:
        return any(lowered.startswith(a.lower()) for a in self.network_allowed_commands if a)

    def _deleted_paths(self, command: str) -> List[str]:
        """从删除命令中提取目标路径（忽略 -f/-r 等选项）。"""
        targets: List[str] = []
        for pat in DELETE_PATTERNS:
            m = pat.search(command)
            if not m:
                continue
            # 取最后一个非空捕获组（尾部路径段，避免误取 -rf 等选项）
            groups = [m.group(i) for i in range(1, (m.lastindex or 0) + 1)]
            rest = next((g for g in reversed(groups) if g), m.group(0))
            # 去掉选项，取第一个非选项 token 作为删除目标
            strip_chars = chr(39) + chr(96) + chr(34)  # ' " `
            for tok in rest.replace("\\", "/").split():
                if tok.startswith("-"):
                    continue
                targets.append(tok.strip(strip_chars))
                break
        return targets

    def _is_protected(self, path: str) -> bool:
        norm = str(path).replace("\\", "/").strip("\"'` ")
        for pattern in self.protected_paths:
            p = str(pattern).replace("\\", "/")
            if p.startswith("/"):
                continue  # 只匹配相对路径片段/模式
            if fnmatch.fnmatch(norm, p) or fnmatch.fnmatch(norm.split("/")[-1], p):
                return True
            if norm.startswith(p.rstrip("/") + "/"):
                return True
            # 含目录的受保护模式：删除其顶层目录同样拦截（如 rm -rf config）
            first_seg = p.split("/")[0]
            if first_seg and norm in (first_seg, first_seg + "/"):
                return True
        return False

    def _audit_network(self, command: str) -> None:
        if self.decision_logger is None:
            return
        urls = URL_PATTERN.findall(command)
        if urls:
            self.decision_logger.record(
                "network.audit", "sandbox.network_policy", self.network_policy,
                f"网络请求审计: {'; '.join(u[:100] for u in urls)}",
            )

    @staticmethod
    def _first_url(command: str) -> str:
        m = URL_PATTERN.search(command)
        return m.group(0).strip("\"'") if m else ""

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