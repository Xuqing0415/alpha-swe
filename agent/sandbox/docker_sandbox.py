"""Docker 沙箱容器生命周期管理 —— 对应设计第 12 节。

- start(): 镜像拉取（缺失时）-> 创建容器（网络隔离/资源限制/只读根/卷挂载）-> 启动；
- exec_run(): 容器内执行命令，超时强制 kill 容器；
- read_file/write_file/append_file/search_file: 经 get_archive/put_archive 与
  grep 操作容器文件系统（路径锚定在 workdir，即 /workspace 卷挂载点）；
- snapshot(): docker commit 保存容器状态；rollback(): 从快照镜像重建容器；
- stop()/cleanup(): 强制移除容器；stats()/status(): 资源采样与状态查询。

依赖 docker-py（pip install docker），仅在 docker_enabled=True 时按需导入；
测试可注入 fake client（client=...）离线验证完整生命周期。
"""
from __future__ import annotations

import asyncio
import io as _io
import logging
import posixpath
import re
import shlex
import tarfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from agent.config import SandboxConfig

logger = logging.getLogger("alpha-swe.sandbox.docker")

# 快照标签只允许 [A-Za-z0-9_.-]，用于镜像 repository/tag
_TAG_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass
class ExecResult:
    """容器内命令执行结果。"""
    exit_code: int
    stdout: str
    stderr: str = ""


class DockerSandbox:
    """沙箱容器生命周期管理器。

    config.docker_enabled=True 时启用；未启用或 docker SDK 缺失时所有方法安全返回
    降级状态（start 返回 ""，exec_run 返回失败 ExecResult），便于本地开发与测试。
    """

    def __init__(self, config: Optional[SandboxConfig] = None,
                 decision_logger=None, client=None):
        self.config = config or SandboxConfig()
        self.decision_logger = decision_logger
        self._client = client  # 可注入 fake client（docker SDK 兼容接口）
        self._container = None
        self._container_id = ""
        self._image = ""
        self._snapshot_stack: List[str] = []
        self._lock = asyncio.Lock()
        self._exec_count = 0
        self._started_ts = 0.0

    # ---- 状态 ----
    @property
    def enabled(self) -> bool:
        return bool(self.config.docker_enabled)

    @property
    def running(self) -> bool:
        return self._container is not None

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "container_id": self._container_id or "",
            "image": self._image or "",
            "snapshots": list(self._snapshot_stack),
            "exec_count": self._exec_count,
        }

    # ---- 内部：docker 客户端 ----
    def _docker(self):
        """惰性加载 docker SDK；已注入 client 时直接返回。"""
        if self._client is not None:
            return self._client
        try:
            import docker
        except ImportError as e:  # pragma: no cover - 环境相关
            raise RuntimeError("Docker 沙箱需要 docker-py: pip install docker") from e
        self._client = docker.from_env()
        return self._client

    def _require_container(self):
        if self._container is None:
            raise RuntimeError("容器未启动（docker_enabled=False 或 start() 未调用）")
        return self._container

    def _log(self, name: str, config_key: str, config_value: Any, decision: str) -> None:
        if self.decision_logger is not None:
            self.decision_logger.record(name, config_key, config_value, decision)

    # ---- 容器规格（配置驱动，供测试/决策日志） ----
    def build_container_spec(self, workspace: str = "./workspace") -> Dict[str, Any]:
        """返回 docker.containers.create 需要的参数子集。"""
        if self.decision_logger is not None:
            self._log("network_mode", "sandbox.network_enabled",
                      self.config.is_network_enabled,
                      f"容器网络模式: {self.config.network_mode}")
            self._log("memory_limit", "sandbox.memory_limit",
                      self.config.memory_limit,
                      f"容器内存限制: {self.config.memory_limit}")
            self._log("cpu_limit", "sandbox.cpu_limit",
                      self.config.cpu_limit,
                      f"容器 CPU 限制: {self.config.cpu_limit}")
            self._log("container_timeout", "sandbox.timeout_seconds",
                      self.config.timeout_seconds,
                      f"容器超时: {self.config.timeout_seconds}s")
            self._log("read_only_root", "sandbox.read_only_root",
                      self.config.read_only_root,
                      f"容器根文件系统只读: {self.config.read_only_root}")
        return {
            "image": self.config.image,
            "network_mode": self.config.network_mode,
            "mem_limit": self.config.memory_limit,
            "nano_cpus": int(self.config.cpu_limit * 1e9),
            "read_only": self.config.read_only_root,
            "timeout_seconds": self.config.timeout_seconds,
            "volumes": {workspace: {"bind": self.config.workdir,
                                    "mode": self.config.volume_mode}},
        }

    # ---- 生命周期 ----
    async def start(self, workspace: str = "./workspace") -> str:
        """拉取镜像（缺失时）并创建/启动容器；返回容器 ID。失败返回 ""。"""
        if self._container is not None:
            return self._container_id
        if not self.enabled:
            self._log("docker.disabled", "sandbox.docker_enabled", False,
                      "Docker 沙箱未启用，跳过容器创建")
            return ""
        try:
            client = self._docker()
            spec = self.build_container_spec(workspace)
            image = await self._ensure_image(client, spec["image"])
            self._image = str(getattr(image, "id", spec["image"]))
            create_kw: Dict[str, Any] = dict(
                image=spec["image"],
                detach=True,
                working_dir=self.config.workdir,
                network_mode=spec["network_mode"],
                mem_limit=spec["mem_limit"],
                nano_cpus=spec["nano_cpus"],
                read_only=spec["read_only"],
                volumes=spec["volumes"],
            )
            if self.config.container_name:
                create_kw["name"] = self.config.container_name
            container = await asyncio.to_thread(
                client.containers.create, **create_kw
            )
            await asyncio.to_thread(container.start)
        except Exception as e:
            logger.warning("Docker 容器启动失败: %s", e)
            self._log("docker.start_failed", "sandbox.docker_enabled", True,
                      f"容器启动失败，降级到本地执行: {str(e)[:120]}")
            return ""
        self._container = container
        self._container_id = str(getattr(container, "id", "unknown"))
        self._started_ts = time.time()
        self._log("docker.start", "sandbox.docker_enabled", True,
                  f"容器已启动 {self._container_id}（镜像 {spec['image']}，"
                  f"网络 {spec['network_mode']}，内存 {spec['mem_limit']}）")
        logger.info("Docker 沙箱容器已启动: %s", self._container_id)
        return self._container_id

    async def _ensure_image(self, client, image: str):
        """镜像缺失时尝试 pull；无法拉取则抛出异常（由 start 兜底降级）。"""
        try:
            return client.images.get(image)
        except Exception:
            logger.info("镜像 %s 不存在，尝试拉取…", image)
            return await asyncio.to_thread(client.images.pull, image)

    async def exec_run(self, command: str, timeout: Optional[float] = None,
                       workdir: Optional[str] = None,
                       environment: Optional[Dict[str, str]] = None) -> ExecResult:
        """容器内执行命令；超时强制 kill 容器并返回失败结果。"""
        if not self.running:
            return ExecResult(exit_code=-1, stdout="", stderr="容器未启动")
        container = self._container
        timeout = timeout or float(self.config.timeout_seconds)
        workdir = workdir or self.config.workdir
        self._exec_count += 1
        self._log("docker.exec", "sandbox.docker_enabled", True,
                  f"容器执行 #{self._exec_count}: {command[:100]}")

        def _run():
            return container.exec_run(
                command, demux=True, workdir=workdir,
                environment=environment or {},
            )

        try:
            res = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
        except asyncio.TimeoutError:
            self._log("docker.timeout", "sandbox.timeout_seconds", timeout,
                      f"容器命令超时({timeout}s)，强制 kill: {command[:100]}")
            try:
                await asyncio.to_thread(container.kill)
            except Exception as e:
                logger.warning("kill 容器失败: %s", e)
            return ExecResult(exit_code=-1, stdout="", stderr=f"命令超时({timeout}s)，容器已强制 kill")
        except Exception as e:
            return ExecResult(exit_code=-1, stdout="", stderr=f"容器执行失败: {e}")

        exit_code = getattr(res, "exit_code", None)
        if exit_code is None:
            exit_code = -1
        exit_code = int(exit_code)
        out, err = res.output if isinstance(res.output, tuple) else (res.output, None)
        return ExecResult(
            exit_code=exit_code,
            stdout=(out or b"").decode("utf-8", errors="replace"),
            stderr=(err or b"").decode("utf-8", errors="replace"),
        )

    # ---- 文件操作（限定 workdir，即 /workspace 卷挂载点） ----
    def _container_path(self, rel_path: str) -> str:
        """相对工作区路径 -> 容器内绝对路径（防穿越）。"""
        clean = str(rel_path).replace(chr(92), "/").lstrip("/")
        if ".." in clean.split("/"):
            raise PermissionError(f"禁止容器内路径穿越: {rel_path}")
        return posixpath.join(self.config.workdir, clean)

    async def read_file(self, rel_path: str) -> str:
        if not self.running:
            raise RuntimeError("容器未启动")
        full = self._container_path(rel_path)
        try:
            tar_stream, _ = await asyncio.to_thread(
                self._container.get_archive, full
            )
            data = b""
            for chunk in tar_stream:
                data += chunk
        except Exception as e:
            raise FileNotFoundError(f"容器内读取失败 {full}: {e}") from e
        return self._extract_tar_first(data)

    async def write_file(self, rel_path: str, content: str) -> None:
        if not self.running:
            raise RuntimeError("容器未启动")
        full = self._container_path(rel_path)
        parent = posixpath.dirname(full)
        tar = self._make_tar(posixpath.basename(full), content.encode("utf-8"))
        await asyncio.to_thread(self._container.put_archive, parent, tar)

    async def append_file(self, rel_path: str, content: str) -> None:
        existing = ""
        try:
            existing = await self.read_file(rel_path)
        except FileNotFoundError:
            existing = ""
        await self.write_file(rel_path, existing + content)

    async def search_file(self, pattern: str, rel_path: str) -> str:
        """容器内 grep 搜索；返回 grep -rIn 风格的命中行。"""
        if not self.running:
            return ""
        full = self._container_path(rel_path)
        cmd = (f"grep -rInE --exclude-dir=.git "
               f"{shlex.quote(pattern)} {shlex.quote(full)} || true")
        res = await self.exec_run(cmd, timeout=min(self.config.timeout_seconds, 60))
        return res.stdout

    @staticmethod
    def _make_tar(filename: str, data: bytes) -> bytes:
        buf = _io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name=filename)
            info.size = len(data)
            tf.addfile(info, _io.BytesIO(data))
        return buf.getvalue()

    @staticmethod
    def _extract_tar_first(data: bytes) -> str:
        with tarfile.open(fileobj=_io.BytesIO(data), mode="r") as tf:
            member = tf.next()
            if member is None or not member.isfile():
                return ""
            raw = tf.extractfile(member)
            return raw.read().decode("utf-8", errors="replace") if raw else ""

    # ---- 快照 / 回滚 ----
    async def snapshot(self, label: str = "snap") -> Optional[str]:
        """docker commit 当前容器为镜像；返回镜像 tag。失败返回 None。"""
        if not self.running:
            return None
        safe = _TAG_SAFE.sub("_", label)[:48]
        repo = self.config.snapshot_prefix
        tag = f"{safe}-{int(time.time())}"
        try:
            await asyncio.to_thread(
                self._container.commit, repository=repo, tag=tag,
            )
        except Exception as e:
            logger.warning("快照失败 %s: %s", label, e)
            self._log("docker.snapshot_failed", "sandbox.snapshot_prefix", repo,
                      f"快照失败 {label}: {str(e)[:120]}")
            return None
        self._snapshot_stack.append(tag)
        self._log("docker.snapshot", "sandbox.snapshot_prefix", repo,
                  f"快照 {label} -> {repo}:{tag}")
        return tag

    async def rollback(self, snapshot: Optional[str] = None) -> bool:
        """从快照镜像重建容器；snapshot=None 时回滚到最近一次快照。"""
        if not self.enabled:
            return False
        async with self._lock:
            tag = snapshot
            if tag is None:
                if not self._snapshot_stack:
                    return False
                tag = self._snapshot_stack.pop()
            image_ref = f"{self.config.snapshot_prefix}:{tag}"
            client = self._docker()
            old = self._container
            self._container = None
            self._container_id = ""
            try:
                if old is not None:
                    await asyncio.to_thread(old.remove, force=True)
                spec = self.build_container_spec(self.config.workspace)
                container = await asyncio.to_thread(
                    client.containers.create,
                    image=image_ref,
                    detach=True,
                    working_dir=self.config.workdir,
                    network_mode=spec["network_mode"],
                    mem_limit=spec["mem_limit"],
                    nano_cpus=spec["nano_cpus"],
                    read_only=spec["read_only"],
                    volumes=spec["volumes"],
                )
                await asyncio.to_thread(container.start)
            except Exception as e:
                logger.warning("回滚失败: %s", e)
                self._log("docker.rollback_failed", "sandbox.auto_rollback", True,
                          f"回滚失败: {str(e)[:120]}")
                return False
            self._container = container
            self._container_id = str(getattr(container, "id", "unknown"))
            self._log("docker.rollback", "sandbox.auto_rollback", True,
                      f"已回滚到快照 {image_ref}，新容器 {self._container_id}")
            return True

    # ---- 清理 / 监控 ----
    async def stop(self, remove: bool = True) -> None:
        if self._container is None:
            return
        container = self._container
        self._container = None
        self._container_id = ""
        try:
            if remove:
                await asyncio.to_thread(container.remove, force=True)
            else:
                try:
                    await asyncio.to_thread(container.stop)
                except Exception:
                    await asyncio.to_thread(container.remove, force=True)
        except Exception as e:
            logger.warning("停止容器异常: %s", e)
        self._log("docker.stop", "sandbox.docker_enabled", True,
                  "沙箱容器已移除")

    cleanup = stop

    async def stats(self) -> Dict[str, Any]:
        """容器资源采样（memory_stats/cpu_stats 简化汇总）；未运行返回空 dict。"""
        if not self.running:
            return {}
        try:
            raw = await asyncio.to_thread(self._container.stats, stream=False)
        except Exception:
            return {}
        mem = raw.get("memory_stats", {}) or {}
        cpu = raw.get("cpu_stats", {}) or {}
        prev_cpu = raw.get("precpu_stats", {}) or {}
        usage = mem.get("usage", 0) or 0
        limit = mem.get("limit", 0) or 0
        return {
            "mem_usage_mb": round(usage / 1024 / 1024, 1),
            "mem_limit_mb": round(limit / 1024 / 1024, 1) if limit else 0,
            "mem_percent": round(usage / limit * 100, 1) if limit else 0.0,
            "cpu_total_usage": cpu.get("cpu_usage", {}).get("total_usage", 0),
            "cpu_prev_total": prev_cpu.get("cpu_usage", {}).get("total_usage", 0),
            "system_cpu": cpu.get("system_cpu_usage", 0),
            "system_prev_cpu": prev_cpu.get("system_cpu_usage", 0),
        }


__all__ = ["DockerSandbox", "ExecResult"]
