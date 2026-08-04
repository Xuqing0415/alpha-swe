"""第六关：Sandbox 环境控制（安全围栏）
所有 file_write 和 terminal_execute 必须通过 Sandbox 检查：
- 文件操作：禁止写入系统目录 + 路径遍历防护 + 相对路径锚定到工作区
- 终端执行：自动追加工作目录前缀，禁止 sudo/rm -rf/chmod 777/dd/mkfs 等危险命令
"""
import os
import re
import logging
from typing import Tuple, List

logger = logging.getLogger("alpha-swe.sandbox")

# 路径遍历模式
PATH_TRAVERSAL_PATTERN = re.compile(r'(?:\.\./|\.\.\\)')


class Sandbox:
    """安全沙箱——路径白名单和命令过滤"""

    # 默认禁止的路径
    DEFAULT_BLOCKED_PATHS = [
        "/etc", "/sys", "/proc", "/boot", "/root", "/dev",
        "/usr/lib", "/usr/bin", "/usr/sbin", "/usr/local/bin",
        "/bin", "/sbin", "/lib", "/lib64",
        "C:\\Windows", "C:\\Windows\\System32", "C:\\Windows\\SysWOW64",
        "C:\\Program Files", "C:\\Program Files (x86)",
        "/System", "/Library", "/Applications",
        "~/.ssh", "~/.gnupg", "~/.aws",
    ]

    # 默认禁止的命令关键词（增强版）
    DEFAULT_BLOCKED_COMMANDS = [
        # 权限提升
        "sudo", "su ",
        # 危险删除
        "rm -rf /", "rm -rf --no-preserve-root", "rm -rf ~",
        # 格式化
        "mkfs", "mke2fs", "mkswap",
        # 磁盘操作
        "dd if=", "dd of=", "fdisk", "parted",
        # fork bomb
        ":(){ :|:& };:",
        # 权限修改
        "chmod 777", "chmod -R 777", "chmod 7777", "chown -R",
        # 写入设备
        "> /dev/sda", "> /dev/hda", "> /dev/sd",
        # 系统控制
        "shutdown", "reboot", "halt", "poweroff", "init 0", "init 6",
        # 网络（按需开放）
        "wget", "curl",
        # 代码执行
        "eval", "exec",
        # 进程注入
        "ptrace", "strace",
        # 内核模块
        "modprobe", "insmod", "rmmod",
    ]

    def __init__(self, workspace: str = "/tmp/workspace",
                 allowed_paths: List[str] = None,
                 blocked_paths: List[str] = None,
                 block_commands: List[str] = None):
        self.workspace = os.path.abspath(workspace)
        os.makedirs(self.workspace, exist_ok=True)
        self.allowed_paths = [os.path.abspath(p) for p in (allowed_paths or [])]
        self.blocked_paths = [os.path.abspath(p) for p in (blocked_paths or self.DEFAULT_BLOCKED_PATHS)]
        self.blocked_commands = block_commands or self.DEFAULT_BLOCKED_COMMANDS
        self.violation_count = 0
        self.violation_log: List[dict] = []

    def check(self, tool_name: str, params: dict) -> Tuple[bool, str]:
        """检查操作是否允许，返回 (允许, 原因)"""
        if tool_name == "file_ops":
            return self._check_file_ops(params)
        elif tool_name == "terminal_execute":
            return self._check_terminal(params)
        else:
            return True, ""

    def resolve_path(self, path: str) -> str:
        """将相对路径解析到沙箱工作区内，绝对路径保持原样"""
        if os.path.isabs(path):
            return os.path.normpath(os.path.abspath(path))
        return os.path.normpath(os.path.join(self.workspace, path))

    def _check_file_ops(self, params: dict) -> Tuple[bool, str]:
        """检查文件操作（含路径遍历防护）"""
        action = params.get("action", "")
        path = params.get("path", "")

        if not path:
            return True, ""

        # 路径遍历检测
        if PATH_TRAVERSAL_PATTERN.search(path):
            self._log_violation("file_ops", f"路径遍历攻击: {path}")
            return False, f"禁止路径遍历: {path}"

        # 相对路径统一锚定到沙箱工作区
        params["path"] = self.resolve_path(path)
        abs_path = params["path"]

        # 读操作宽松
        if action == "read":
            return True, ""

        # 写操作严格检查
        if action in ("write", "append"):
            # 检查是否在禁止路径列表中
            for blocked in self.blocked_paths:
                blocked_abs = os.path.normpath(os.path.abspath(blocked))
                if abs_path.startswith(blocked_abs):
                    self._log_violation("file_ops", f"写入 {abs_path} 被拦截（禁止路径: {blocked}）")
                    return False, f"禁止写入系统目录: {blocked}"

            # 检查是否在允许路径中
            if self.allowed_paths:
                in_allowed = False
                for allowed in self.allowed_paths:
                    allowed_abs = os.path.normpath(os.path.abspath(allowed))
                    if abs_path.startswith(allowed_abs):
                        in_allowed = True
                        break
                if not in_allowed:
                    self._log_violation("file_ops", f"写入 {abs_path} 不在白名单中")
                    return False, f"路径不在白名单中: {abs_path}"

        return True, ""

    def _check_terminal(self, params: dict) -> Tuple[bool, str]:
        """检查终端命令"""
        command = params.get("command", "")

        # 检查危险命令
        cmd_lower = command.lower()
        for blocked in self.blocked_commands:
            if blocked.lower() in cmd_lower:
                self._log_violation("terminal_execute", f"命令包含禁止关键词: {blocked}")
                return False, f"禁止执行危险命令（包含: {blocked}）"

        # 非空命令且未显式 cd 时，自动追加工作目录前缀
        stripped = command.strip()
        if stripped and not stripped.startswith("cd "):
            # Windows 使用 PowerShell（; 分隔），Unix 使用 bash（&& 分隔）
            if os.name == "nt":
                params["command"] = f"cd {self.workspace}; {command}"
            else:
                params["command"] = f"cd {self.workspace} && {command}"

        return True, ""

    def _log_violation(self, tool: str, reason: str):
        """记录违规"""
        self.violation_count += 1
        self.violation_log.append({
            "tool": tool,
            "reason": reason,
            "count": self.violation_count
        })
        logger.warning(f"[Sandbox] 违规 #{self.violation_count}: {reason}")

    def get_stats(self) -> dict:
        """获取沙箱统计"""
        return {
            "violation_count": self.violation_count,
            "recent_violations": self.violation_log[-5:],
            "blocked_paths": self.blocked_paths[:5],
            "blocked_commands": self.blocked_commands[:5],
            "workspace": self.workspace
        }