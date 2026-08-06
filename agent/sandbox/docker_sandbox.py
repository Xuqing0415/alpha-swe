"""Docker 沙箱配置驱动 —— 由 SandboxConfig 生成容器创建参数（docker-py 预留）。

对应设计 12 节：镜像、网络模式、内存/CPU 限制、根文件系统只读、超时。
docker_enabled=True 时由运行时调用 build_container_spec() 并创建容器；
本模块不直接依赖 docker SDK，便于离线测试与决策日志验证。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agent.config import SandboxConfig

logger = logging.getLogger("alpha-swe.sandbox.docker")


class DockerSandbox:
    """把沙箱配置翻译为容器规格，并在构建前记录网络/资源决策。"""

    def __init__(self, config: Optional[SandboxConfig] = None,
                 decision_logger=None):
        self.config = config or SandboxConfig()
        self.decision_logger = decision_logger

    def build_container_spec(self, workspace: str = "./workspace") -> Dict[str, Any]:
        """返回 docker.containers.create 需要的参数子集。"""
        if self.decision_logger is not None:
            self.decision_logger.record(
                "network_mode", "sandbox.network_enabled",
                self.config.is_network_enabled,
                f"容器网络模式: {self.config.network_mode}",
            )
            self.decision_logger.record(
                "memory_limit", "sandbox.memory_limit",
                self.config.memory_limit,
                f"容器内存限制: {self.config.memory_limit}",
            )
            self.decision_logger.record(
                "cpu_limit", "sandbox.cpu_limit",
                self.config.cpu_limit,
                f"容器 CPU 限制: {self.config.cpu_limit}",
            )
            self.decision_logger.record(
                "container_timeout", "sandbox.timeout_seconds",
                self.config.timeout_seconds,
                f"容器超时: {self.config.timeout_seconds}s",
            )
            self.decision_logger.record(
                "read_only_root", "sandbox.read_only_root",
                self.config.read_only_root,
                f"容器根文件系统只读: {self.config.read_only_root}",
            )
        return {
            "image": self.config.image,
            "network_mode": self.config.network_mode,
            "mem_limit": self.config.memory_limit,
            "nano_cpus": int(self.config.cpu_limit * 1e9),
            "read_only": self.config.read_only_root,
            "timeout_seconds": self.config.timeout_seconds,
            "volumes": {workspace: {"bind": "/workspace", "mode": "rw"}},
        }


__all__ = ["DockerSandbox"]