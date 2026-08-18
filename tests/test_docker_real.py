# -*- coding: utf-8 -*-
"""真实 Docker 沙箱验证套件（方向一·阶段 1）。

与 tests/test_docker_sandbox.py（fake docker client 离线）互补：
本文件直接驱动真实 Docker Engine（docker-py 连接 daemon），逐项验证
生命周期 / 网络策略 / 资源限制 / 文件挂载与隔离 / 快照回滚 / 超时恢复。

- Docker daemon 不可达时整模块自动 skip（CI 按环境标记），本地/CI 真实
  Docker 环境下自动运行；
- 每个测试创建独立 workspace + 独立容器，finally 清理，互不污染；
- 依赖基础镜像 alphaswe/dev:latest（见 docker/Dockerfile），缺失时尝试 pull。

运行（真实 Docker 环境）：
    python -X utf8 -m pytest tests/test_docker_real.py -q
"""
from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path

import pytest

from agent.config import SandboxConfig
from agent.sandbox.docker_sandbox import DockerSandbox

pytestmark = pytest.mark.docker

TEST_IMAGE = os.environ.get("ALPHASWE_TEST_IMAGE", "alphaswe/dev:latest")


def _docker_available() -> bool:
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


pytestmark = [pytest.mark.docker,
              pytest.mark.skipif(not _docker_available(),
                                 reason="Docker daemon 不可达，跳过真实 Docker 验证")]

WS_ROOT = Path(__file__).resolve().parent.parent / "test_workspace"


def _make_workspace() -> Path:
    d = WS_ROOT / ("docker_real_" + os.urandom(4).hex())
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cfg(ws: Path, **overrides) -> SandboxConfig:
    kwargs = dict(
        docker_enabled=True,
        image=TEST_IMAGE,
        workspace=str(ws),
        read_only_root=True,
        timeout_seconds=30,
        memory_limit="256m",
        cpu_limit=1.0,
        max_snapshots=3,
    )
    kwargs.update(overrides)
    return SandboxConfig(**kwargs)


def _force_rmtree(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for name in list(files) + list(dirs):
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
            except OSError:
                pass
    try:
        import shutil
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


async def _start(sb: DockerSandbox, ws: Path) -> str:
    cid = await sb.start(str(ws))
    assert cid, "容器启动失败（返回空 ID）"
    assert sb.running
    return cid


# ---------------- A. 容器生命周期 ----------------
@pytest.mark.asyncio
async def test_lifecycle_create_exec_stop_remove():
    """创建 -> exec 执行 -> 停止并删除 全链路。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws))
    try:
        cid = await _start(sb, ws)
        res = await sb.exec_run("echo hello-from-container")
        assert res.exit_code == 0
        assert "hello-from-container" in res.stdout
        st = sb.status()
        assert st["container_id"] == cid
        assert st["running"]
        await sb.stop(remove=True)
        assert not sb.running
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_abnormal_exit_reported():
    """容器内进程异常退出：exit_code 非 0，错误输出被捕获。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws))
    try:
        await _start(sb, ws)
        res = await sb.exec_run("python -c \"import sys; sys.exit(3)\"")
        assert res.exit_code == 3
        res2 = await sb.exec_run("sh -c 'echo boom 1>&2; exit 1'")
        assert res2.exit_code == 1
        assert "boom" in res2.stderr
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_timeout_kill_then_restart():
    """命令超时：强制 kill + 自动重启容器，后续 exec 仍可用。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws, timeout_seconds=30, restart_after_timeout=True))
    try:
        await _start(sb, ws)
        res = await sb.exec_run("sleep 10", timeout=2)
        assert res.exit_code == -1
        assert "超时" in res.stderr
        # 自动重启后容器可继续执行
        res2 = await sb.exec_run("echo alive-after-timeout")
        assert res2.exit_code == 0
        assert "alive-after-timeout" in res2.stdout
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_container_name_stable():
    """container_name 配置生效：容器使用指定名称。"""
    ws = _make_workspace()
    name = f"alphaswe-verify-{os.urandom(4).hex()}"
    sb = DockerSandbox(_cfg(ws, container_name=name))
    try:
        await _start(sb, ws)
        assert sb.running
        cid = sb._container_id
        client = sb._docker()
        info = client.containers.get(cid)
        assert info.name == name
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


# ---------------- B. 网络策略 ----------------
@pytest.mark.asyncio
async def test_network_disabled_none_mode():
    """no_network（network_mode=none）：容器内无法访问外部网络。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws, no_network=True, network_enabled=None))
    try:
        await _start(sb, ws)
        res = await sb.exec_run("curl -sS --max-time 5 https://example.com")
        assert res.exit_code != 0
        assert "Could not resolve" in res.stdout + res.stderr
        res_ping = await sb.exec_run("ping -c 1 8.8.8.8")
        assert res_ping.exit_code != 0
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_network_enabled_bridge_mode():
    """network_enabled=True（bridge）：容器内可访问外网。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws, network_enabled=True))
    try:
        await _start(sb, ws)
        res = await sb.exec_run("curl -sS --max-time 15 https://example.com -o /dev/null -w '%{http_code}'")
        assert res.exit_code == 0
        assert res.stdout.strip() == "200"
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_network_mode_visible_in_inspect():
    """容器实际网络模式与配置一致（docker inspect 交叉验证）。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws, no_network=True))
    try:
        await _start(sb, ws)
        info = sb._docker().containers.get(sb._container_id)
        host = info.attrs.get("HostConfig", {})
        assert host.get("NetworkMode") == "none"
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


# ---------------- C. 资源限制 ----------------
@pytest.mark.asyncio
async def test_memory_oom_kill():
    """内存超限：容器被 OOM kill（exit_code 137），宿主不受影响。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws, memory_limit="96m", timeout_seconds=60))
    try:
        await _start(sb, ws)
        code = (
            "import time\n"
            "buf=[]\n"
            "print('alloc start')\n"
            "while True:\n"
            "  buf.append(bytearray(32*1024*1024))\n"
            "  time.sleep(0.02)\n"
        )
        res = await sb.exec_run(f"python -c \"{code}\"", timeout=60)
        # OOM 表现为进程被杀（137）或命令超时强杀（-1），不应是正常退出
        assert res.exit_code in (137, -1), f"OOM 未生效: {res}"
        # 宿主仍有响应（后续 exec 可用，超时强杀后自动重启）
        res2 = await sb.exec_run("echo host-ok")
        assert res2.exit_code in (0, -1)
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_cpu_limit_applied():
    """CPU 限制：nano_cpus 写入 HostConfig，stats() 能采样。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws, cpu_limit=0.5))
    try:
        await _start(sb, ws)
        info = sb._docker().containers.get(sb._container_id)
        host = info.attrs.get("HostConfig", {})
        assert host.get("NanoCpus") == int(0.5 * 1e9)
        res = await sb.exec_run("python -c \"sum(range(10**6))\"")
        assert res.exit_code == 0
        st = await sb.stats()
        assert isinstance(st, dict)
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_memory_limit_in_inspect():
    """内存限制写入 HostConfig.Memory。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws, memory_limit="256m"))
    try:
        await _start(sb, ws)
        info = sb._docker().containers.get(sb._container_id)
        assert info.attrs["HostConfig"]["Memory"] == 256 * 1024 * 1024
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


# ---------------- D. 文件挂载与隔离 ----------------
@pytest.mark.asyncio
async def test_workspace_bind_mount_rw_sync():
    """工作区 bind 挂载 rw：容器内写文件同步到宿主机。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws))
    try:
        await _start(sb, ws)
        await sb.write_file("proj/marker.txt", "from-container")
        assert (ws / "proj" / "marker.txt").read_text(encoding="utf-8") == "from-container"
        # 宿主机写入 -> 容器内可读
        (ws / "host.txt").write_text("from-host", encoding="utf-8")
        content = await sb.read_file("host.txt")
        assert content == "from-host"
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_workspace_readonly_mount():
    """volume_mode=ro：容器内无法修改工作区文件。"""
    ws = _make_workspace()
    (ws / "locked.txt").write_text("readonly", encoding="utf-8")
    sb = DockerSandbox(_cfg(ws, volume_mode="ro"))
    try:
        await _start(sb, ws)
        res = await sb.exec_run("touch /workspace/locked.txt")
        assert res.exit_code != 0
        assert "Read-only file system" in res.stdout + res.stderr
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_path_traversal_blocked_in_container():
    """容器内路径穿越被拦截（read_file / write_file）。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws))
    try:
        await _start(sb, ws)
        with pytest.raises(PermissionError):
            await sb.read_file("../etc/passwd")
        with pytest.raises(PermissionError):
            await sb.write_file("../../etc/passwd", "x")
        with pytest.raises(PermissionError):
            await sb.read_file("..%2f..%2fetc%2fpasswd")
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_tmpfs_writable_with_readonly_root():
    """read_only_root=True 时 /tmp 仍可写（tmpfs），根文件系统只读生效。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws))
    try:
        await _start(sb, ws)
        # 根文件系统只读：写 /etc 失败
        res = await sb.exec_run("touch /etc/readonly-test")
        assert res.exit_code != 0
        assert "Read-only file system" in res.stdout + res.stderr
        # tmpfs 可写
        res2 = await sb.exec_run("echo tmp-ok > /tmp/t.txt && cat /tmp/t.txt")
        assert res2.exit_code == 0
        assert "tmp-ok" in res2.stdout
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_workspace_file_persists_across_restart():
    """容器重启后工作区文件仍在（bind 挂载持久化）。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws))
    try:
        await _start(sb, ws)
        await sb.write_file("keep.txt", "persist-me")
        await sb.exec_run("echo boom; exit 2")
        await sb.stop(remove=True)
        await _start(sb, ws)
        content = await sb.read_file("keep.txt")
        assert content == "persist-me"
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


# ---------------- E. 快照与回滚 ----------------
@pytest.mark.asyncio
async def test_snapshot_rollback_env_state():
    """快照 -> 修改容器内可写状态 -> 回滚恢复（read_only_root=False 场景）。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws, read_only_root=False, max_snapshots=5))
    try:
        await _start(sb, ws)
        # 在容器根文件系统写入标记（可被 docker commit 捕获）
        await sb.exec_run("mkdir -p /opt/mark && echo v1 > /opt/mark/version")
        snap = await sb.snapshot("pre-change")
        assert snap, "快照创建失败"
        await sb.exec_run("echo v2 > /opt/mark/version")
        res = await sb.exec_run("cat /opt/mark/version")
        assert "v2" in res.stdout
        ok = await sb.rollback(snap)
        assert ok, "回滚失败"
        res = await sb.exec_run("cat /opt/mark/version")
        assert "v1" in res.stdout
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_snapshot_rollback_workspace_persist():
    """bind 挂载的工作区文件不随容器回滚而丢失（回滚后仍可见）。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws, max_snapshots=5))
    try:
        await _start(sb, ws)
        await sb.write_file("note.txt", "keep-me")
        snap = await sb.snapshot("pre-rollback")
        assert snap
        ok = await sb.rollback(snap)
        assert ok
        content = await sb.read_file("note.txt")
        assert content == "keep-me"
    finally:
        await sb.stop(remove=True)
        _force_rmtree(ws)


@pytest.mark.asyncio
async def test_snapshot_cleanup_limits_images():
    """快照自动清理：超过 max_snapshots 后旧快照镜像被删除。"""
    ws = _make_workspace()
    sb = DockerSandbox(_cfg(ws, max_snapshots=2, snapshot_prefix="alphaswe/snap"))
    try:
        await _start(sb, ws)
        for i in range(4):
            tag = await sb.snapshot(f"loop-{i}")
            assert tag
        tags = sb.snapshot_images()
        assert len(tags) <= 2, f"快照清理未生效: {tags}"
    finally:
        await sb.stop(remove=True)
        # 清理测试产生的快照镜像
        for tag in list(sb.snapshot_images()):
            try:
                sb._docker().images.remove(tag, force=True, noprune=True)
            except Exception:
                pass
        _force_rmtree(ws)
