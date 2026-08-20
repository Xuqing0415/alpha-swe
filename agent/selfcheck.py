"""启动自检 —— CLI/TUI 启动时验证关键依赖，任务开始前暴露配置/环境问题。

对应「命令行/TUI 驱动新核心系统性排查」第一步 1.3，检查项：
- config：配置最小完整性（必需段已加载）；
- workspace：工作区存在/可创建/可写；
- memory：记忆后端可用（失败允许降级到无记忆模式，非关键）；
- sandbox：策略加载 + 危险命令/越界路径拦截自检；
- tools：核心工具集完整实例化并注册；
- observability：trace/档案目录可写，Web 面板端口可用（非关键）。

所有检查项不向外抛异常：单项失败标记 ok=False 并给出可读原因，
由调用方决定继续（非关键项）或退出（关键项）。
"""
from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("alpha-swe.selfcheck")

_CRITICAL_CHECKS = {"config", "workspace", "sandbox", "tools"}


@dataclass
class SelfCheckItem:
    name: str
    ok: bool
    detail: str = ""
    critical: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "critical": self.critical}


def _check_config(cfg) -> SelfCheckItem:
    """配置完整性：必需段已加载。"""
    try:
        for name in ("agent", "sandbox", "llm", "memory", "context", "mcp",
                     "tools", "planner"):
            if getattr(cfg, name, None) is None:
                return SelfCheckItem("config", False,
                                     "配置段缺失: %s" % name, critical=True)
        return SelfCheckItem("config", True,
                             "配置加载正常（%s）" % type(cfg).__name__)
    except Exception as e:
        return SelfCheckItem("config", False, "配置校验失败: %s" % e,
                             critical=True)


def _check_workspace(cfg) -> SelfCheckItem:
    """工作区存在/可创建/可写。"""
    ws = getattr(cfg.sandbox, "workspace", "./workspace")
    try:
        p = Path(ws).expanduser()
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        if not p.is_dir():
            return SelfCheckItem("workspace", False,
                                 "工作区不是目录: %s" % ws, critical=True)
        if not os.access(p, os.W_OK):
            return SelfCheckItem("workspace", False,
                                 "工作区不可写: %s" % ws, critical=True)
        return SelfCheckItem("workspace", True, ws)
    except Exception as e:
        return SelfCheckItem("workspace", False, "工作区不可用: %s" % e,
                             critical=True)


def _check_memory(cfg) -> SelfCheckItem:
    """记忆后端可用性；失败允许降级为无记忆模式（非关键）。"""
    backend = getattr(cfg.memory, "backend", "none")
    if backend in ("none", "off"):
        return SelfCheckItem("memory", True, "记忆已禁用（backend=none）")
    try:
        from agent.memory.factory import build_memory
        from agent.memory.store import NoopMemoryStore
        store = build_memory(cfg.memory)
        if isinstance(store, NoopMemoryStore):
            return SelfCheckItem("memory", False,
                                 "记忆后端初始化失败，已降级到无记忆模式")
        return SelfCheckItem("memory", True, "后端 %s 就绪" % backend)
    except Exception as e:
        return SelfCheckItem("memory", False, "记忆初始化异常: %s" % e)


def _check_sandbox(cfg) -> SelfCheckItem:
    """沙箱策略加载 + 危险命令/越界路径拦截自检。"""
    try:
        from agent.sandbox.policy import SandboxPolicy
        from agent.tools.base import ExecutionContext
        policy = SandboxPolicy(
            workspace=cfg.sandbox.workspace,
            allowed_paths=cfg.sandbox.allowed_paths,
            blocked_paths=cfg.sandbox.blocked_paths,
            block_commands=cfg.sandbox.block_commands,
            network_enabled=cfg.sandbox.is_network_enabled,
            network_policy=cfg.sandbox.network_policy,
            network_allowed_commands=cfg.sandbox.network_allowed_commands,
            fake_network=cfg.sandbox.fake_network,
        )
        ctx = ExecutionContext(workspace=cfg.sandbox.workspace)
        allowed, _reason = policy.check(
            "terminal_execute", {"command": "sudo rm -rf /"}, ctx)
        if allowed:
            return SelfCheckItem("sandbox", False,
                                 "危险命令 sudo rm -rf / 未被拦截",
                                 critical=True)
        outside = str(Path(cfg.sandbox.workspace).resolve().parent
                      / "swe_outside_probe")
        allowed2, _r2 = policy.check(
            "file_ops", {"action": "write", "path": outside}, ctx)
        if allowed2:
            return SelfCheckItem("sandbox", False,
                                 "越界路径未被拦截: %s" % outside,
                                 critical=True)
        return SelfCheckItem("sandbox", True,
                             "策略加载正常（危险命令与越界路径拦截生效）")
    except Exception as e:
        return SelfCheckItem("sandbox", False, "沙箱策略加载失败: %s" % e,
                             critical=True)


def _check_tools(cfg) -> SelfCheckItem:
    """核心工具集完整实例化并注册（与 AgentLoop._default_tools 对齐）。"""
    try:
        from agent.sandbox.policy import SandboxPolicy
        from agent.tools.background import BackgroundTaskTool
        from agent.tools.database_tool import DatabaseTool
        from agent.tools.dependency_tool import DependencyTool
        from agent.tools.fileio import FileAuditStore, FileIOTool
        from agent.tools.git_tool import GitTool
        from agent.tools.manager import ToolManager
        from agent.tools.terminal import TerminalTool
        from agent.tools.test_tool import TestRunnerTool
        policy = SandboxPolicy(
            workspace=cfg.sandbox.workspace,
            allowed_paths=cfg.sandbox.allowed_paths,
            blocked_paths=cfg.sandbox.blocked_paths,
            block_commands=cfg.sandbox.block_commands,
            network_enabled=cfg.sandbox.is_network_enabled,
        )
        manager = ToolManager(policy=policy, default_timeout=30.0)
        manager.register(TerminalTool(
            default_timeout=cfg.tools.terminal_execute.timeout))
        manager.register(FileIOTool(
            audit_store=FileAuditStore(cfg.sandbox.audit_dir)))
        manager.register(TestRunnerTool(
            default_timeout=cfg.tools.test_runner.timeout))
        manager.register(BackgroundTaskTool())
        manager.register(GitTool())
        manager.register(DatabaseTool())
        manager.register(DependencyTool())
        names = set(manager.names())
        expect = {"terminal_execute", "file_ops", "run_tests",
                  "background_task", "git_ops", "database", "dependency"}
        missing = expect - names
        if missing:
            return SelfCheckItem("tools", False,
                                 "工具注册缺失: %s" % sorted(missing),
                                 critical=True)
        return SelfCheckItem("tools", True, "%d 个工具就绪" % len(names))
    except Exception as e:
        return SelfCheckItem("tools", False, "工具初始化失败: %s" % e,
                             critical=True)


def _port_free(host: str, port: int) -> bool:
    """探测端口是否可绑定（占用则返回 False）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_free_port(host: str, start: int, max_tries: int = 20) -> int:
    """从 start 起探测可用端口；全部占用时返回 0（调用方决定是否放弃）。"""
    for port in range(start, start + max_tries):
        if _port_free(host, port):
            return port
    return 0


def _check_observability(cfg) -> SelfCheckItem:
    """可观测性：trace/档案目录可写，Web 面板端口可用（非关键）。"""
    try:
        for d in (getattr(cfg.agent, "trace_dir", "logs/traces"),
                  getattr(cfg.agent, "session_archive_dir", "logs/sessions")):
            Path(d).mkdir(parents=True, exist_ok=True)
        if getattr(cfg.agent, "web_panel_enabled", False):
            port = int(getattr(cfg.agent, "web_panel_port", 8765))
            host = getattr(cfg.agent, "web_panel_host", "127.0.0.1")
            if not _port_free(host, port):
                return SelfCheckItem("observability", False,
                                     "Web 面板端口 %s:%d 已被占用"
                                     % (host, port))
        return SelfCheckItem("observability", True, "trace/档案目录可写")
    except Exception as e:
        return SelfCheckItem("observability", False,
                             "可观测性初始化失败: %s" % e)


_CHECKERS: List[tuple] = [
    ("config", _check_config, True),
    ("workspace", _check_workspace, True),
    ("memory", _check_memory, False),
    ("sandbox", _check_sandbox, True),
    ("tools", _check_tools, True),
    ("observability", _check_observability, False),
]


def run_selfcheck(cfg, checks: Optional[List[str]] = None
                  ) -> List[SelfCheckItem]:
    """执行启动自检；任何单项/整体异常都降级为 FAIL 项，绝不外抛。"""
    results: List[SelfCheckItem] = []
    for name, fn, critical in _CHECKERS:
        if checks and name not in checks:
            continue
        try:
            results.append(fn(cfg))
        except Exception as e:
            results.append(SelfCheckItem(
                name, False, "自检执行异常: %s" % e, critical=critical))
    return results


def critical_failed(results: List[SelfCheckItem]) -> List[SelfCheckItem]:
    """返回未通过的关键检查项（调用方据此决定是否继续）。"""
    return [r for r in results if not r.ok and r.critical]


def format_selfcheck(results: List[SelfCheckItem]) -> str:
    """人类可读的自检摘要（CLI 输出 / TUI 状态栏共用）。"""
    lines = ["启动自检:"]
    for r in results:
        if r.ok:
            mark = "OK  "
        else:
            mark = "FAIL" if r.critical else "WARN"
        lines.append("  [%s] %s: %s" % (mark, r.name, r.detail))
    failed = critical_failed(results)
    lines.append("自检%s" % ("通过"
                             if not failed
                             else "失败（%d 项关键检查未通过）" % len(failed)))
    return "\n".join(lines)