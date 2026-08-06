"""Docker 沙箱容器生命周期测试（fake docker client 离线验证）。

覆盖：容器规格（网络/资源/只读/卷挂载）、start/exec_run/文件读写、
超时 kill、镜像拉取、快照/回滚、stats、stop，以及 AgentLoop 端到端
（容器内执行命令 + 任务前快照 + 失败自动回滚 + close 清理）。
"""
import io as _io
import tarfile
import time

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.decision_logger import DecisionLogger
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.sandbox.docker_sandbox import DockerSandbox


# ---------------- fake docker client ----------------
class FakeImage:
    def __init__(self, id):
        self.id = id


class FakeExecResult:
    def __init__(self, exit_code, output):
        self.exit_code = exit_code
        self.output = output


class FakeImages:
    def __init__(self, client):
        self.client = client

    def get(self, image):
        if image in self.client._images:
            return self.client._images[image]
        raise Exception(f"ImageNotFound: {image}")

    def pull(self, image):
        self.client._pulled.append(image)
        img = FakeImage(image)
        self.client._images[image] = img
        return img


class FakeContainer:
    def __init__(self, image, client):
        self.id = f"cid-{len(client._created) + 1}"
        self.image = image
        self.client = client
        self.fs = {}
        self.started = False
        self.killed = False
        self.removed = False
        self.exec_calls = []
        self.create_kw = {}

    def start(self):
        self.started = True

    def exec_run(self, command, demux=False, workdir=None, environment=None,
                 socket=False):
        self.exec_calls.append(command)
        cmd = str(command).strip()
        if cmd.startswith("echo "):
            return FakeExecResult(0, (cmd[5:].strip().encode() + b"\n", b""))
        if "sleep" in cmd:
            time.sleep(0.4)
            return FakeExecResult(0, (b"", b""))
        if cmd.startswith("grep "):
            hits = []
            for path, data in self.fs.items():
                for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                    hits.append(f"{path}:{i}: {line}")
            out = ("\n".join(hits) + "\n") if hits else ""
            return FakeExecResult(0, (out.encode(), b""))
        return FakeExecResult(0, (b"", b"unknown command"))

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = True
        self.client.removed.append(self.id)

    def commit(self, repository=None, tag="latest", **kw):
        ref = f"{repository}:{tag}"
        img = FakeImage(ref)
        self.client._images[ref] = img
        self.client.commits.append(ref)
        return img

    def get_archive(self, path):
        data = self.fs.get(path)
        if data is None:
            raise Exception(f"file not found: {path}")
        buf = _io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name=path.rsplit("/", 1)[-1])
            info.size = len(data)
            tf.addfile(info, _io.BytesIO(data))
        return iter([buf.getvalue()]), {"size": len(data)}

    def put_archive(self, dest, data):
        import posixpath
        with tarfile.open(fileobj=_io.BytesIO(data), mode="r") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    content = tf.extractfile(m).read()
                    self.fs[posixpath.join(dest, m.name)] = content

    def stats(self, stream=False):
        return {
            "memory_stats": {"usage": 64 * 1024 * 1024, "limit": 2 * 1024 ** 3},
            "cpu_stats": {"cpu_usage": {"total_usage": 1000}, "system_cpu_usage": 2000},
            "precpu_stats": {"cpu_usage": {"total_usage": 500}, "system_cpu_usage": 1000},
        }


class FakeContainers:
    def __init__(self, client):
        self.client = client

    def create(self, image, **kw):
        c = FakeContainer(image, self.client)
        c.create_kw = kw
        self.client._created.append(c)
        self.client.current = c
        return c


class FakeDockerClient:
    def __init__(self, image="alphaswe/dev:latest"):
        self.image = image
        self._images = {image: FakeImage(image)}
        self._created = []
        self.removed = []
        self.commits = []
        self._pulled = []
        self.current = None
        self.containers = FakeContainers(self)
        self.images = FakeImages(self)


def make_sandbox(dl=None, **cfg_kw):
    cfg = SandboxConfig(docker_enabled=True, workspace="./workspace", **cfg_kw)
    client = FakeDockerClient(image=cfg.image)
    return DockerSandbox(cfg, dl, client=client), client


# ---------------- 容器规格与生命周期 ----------------
@pytest.mark.asyncio
async def test_start_creates_container_with_spec():
    dl = DecisionLogger()
    sb, client = make_sandbox(dl)
    cid = await sb.start("./workspace")
    assert cid and sb.running
    c = client._created[0]
    assert c.started
    assert c.image == "alphaswe/dev:latest"
    assert c.create_kw["network_mode"] == "none"      # 默认禁网
    assert c.create_kw["mem_limit"] == "2g"
    assert c.create_kw["nano_cpus"] == 2_000_000_000
    assert c.create_kw["read_only"] is True
    assert c.create_kw["volumes"]["./workspace"] == {"bind": "/workspace", "mode": "rw"}
    assert c.create_kw["working_dir"] == "/workspace"
    assert any(dp.name == "docker.start" for dp in dl.decisions)


@pytest.mark.asyncio
async def test_network_bridge_when_enabled():
    sb, client = make_sandbox(network_enabled=True)
    await sb.start("./workspace")
    assert client._created[0].create_kw["network_mode"] == "bridge"


@pytest.mark.asyncio
async def test_image_pulled_when_missing():
    sb, client = make_sandbox()
    client._images.clear()  # 镜像缺失 -> 触发 pull
    await sb.start("./workspace")
    assert client._pulled == ["alphaswe/dev:latest"]


@pytest.mark.asyncio
async def test_disabled_skips_container():
    dl = DecisionLogger()
    cfg = SandboxConfig(docker_enabled=False)
    sb = DockerSandbox(cfg, dl, client=FakeDockerClient())
    cid = await sb.start("./workspace")
    assert cid == "" and not sb.running
    assert any(dp.name == "docker.disabled" for dp in dl.decisions)


# ---------------- exec ----------------
@pytest.mark.asyncio
async def test_exec_run_in_container():
    dl = DecisionLogger()
    sb, client = make_sandbox(dl)
    await sb.start("./workspace")
    res = await sb.exec_run("echo hello-docker")
    assert res.exit_code == 0
    assert "hello-docker" in res.stdout
    assert client._created[0].exec_calls == ["echo hello-docker"]
    assert any(dp.name == "docker.exec" for dp in dl.decisions)


@pytest.mark.asyncio
async def test_exec_timeout_kills_container():
    dl = DecisionLogger()
    sb, client = make_sandbox(dl)
    await sb.start("./workspace")
    res = await sb.exec_run("sleep 5", timeout=0.1)
    assert res.exit_code == -1
    assert "超时" in res.stderr
    assert client._created[0].killed
    assert any(dp.name == "docker.timeout" for dp in dl.decisions)


# ---------------- 容器内文件 ----------------
@pytest.mark.asyncio
async def test_file_roundtrip_in_container():
    sb, client = make_sandbox()
    await sb.start("./workspace")
    await sb.write_file("src/app.py", "print(1)\n")
    assert await sb.read_file("src/app.py") == "print(1)\n"
    await sb.append_file("src/app.py", "# tail\n")
    assert await sb.read_file("src/app.py") == "print(1)\n# tail\n"
    assert client._created[0].fs["/workspace/src/app.py"] == b"print(1)\n# tail\n"


@pytest.mark.asyncio
async def test_container_path_traversal_blocked():
    sb, client = make_sandbox()
    await sb.start("./workspace")
    with pytest.raises(PermissionError):
        await sb.read_file("../../etc/passwd")


# ---------------- 快照 / 回滚 ----------------
@pytest.mark.asyncio
async def test_snapshot_commit_and_rollback():
    dl = DecisionLogger()
    sb, client = make_sandbox(dl)
    await sb.start("./workspace")
    old_cid = sb._container_id
    tag = await sb.snapshot("pre-t1")
    assert tag and tag.startswith("pre-t1-")
    assert client.commits and client.commits[0] == f"alphaswe/snap:{tag}"
    await sb.write_file("a.txt", "changed")
    assert await sb.rollback(tag) is True
    assert sb.running
    assert client.removed == [old_cid]
    assert client._created[-1].image == f"alphaswe/snap:{tag}"
    assert any(dp.name == "docker.snapshot" for dp in dl.decisions)
    assert any(dp.name == "docker.rollback" for dp in dl.decisions)


@pytest.mark.asyncio
async def test_rollback_latest_snapshot():
    sb, client = make_sandbox()
    await sb.start("./workspace")
    await sb.snapshot("s1")
    await sb.snapshot("s2")
    assert await sb.rollback() is True
    assert "s2-" in client._created[-1].image


# ---------------- 清理 / 监控 ----------------
@pytest.mark.asyncio
async def test_stats_and_stop():
    dl = DecisionLogger()
    sb, client = make_sandbox(dl)
    await sb.start("./workspace")
    st = await sb.stats()
    assert st["mem_usage_mb"] == 64.0
    await sb.stop()
    assert not sb.running
    assert client.removed
    assert any(dp.name == "docker.stop" for dp in dl.decisions)


# ---------------- 主循环端到端 ----------------
class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_loop_config(ws_tmp, **sandbox_kw):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2),
        sandbox=SandboxConfig(docker_enabled=True, workspace=str(ws_tmp / "ws"),
                              **sandbox_kw),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


@pytest.mark.asyncio
async def test_loop_routes_terminal_into_container(ws_tmp):
    dl = DecisionLogger()
    cfg = make_loop_config(ws_tmp)
    docker, client = make_sandbox(dl)
    llm = ScriptedLLM(
        '{"tool": "terminal_execute", "params": {"command": "echo in-container"}}',
        '{"final_answer": "完成"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(),
                     docker_sandbox=docker, decision_logger=dl)
    try:
        result = await loop.run("容器任务")
        assert result.ok
        assert docker.running
        # 终端命令进入了容器（fake exec）
        assert client._created[-1].exec_calls == ["echo in-container"]
        # 任务执行前有快照
        assert client.commits
        assert any(dp.name == "docker.snapshot" for dp in dl.decisions)
        assert any(dp.name == "docker_enabled" for dp in dl.decisions)
    finally:
        await loop.close()
    assert not docker.running
    assert any(dp.name == "docker.stop" for dp in dl.decisions)


@pytest.mark.asyncio
async def test_loop_rollback_on_task_failure(ws_tmp):
    cfg = make_loop_config(ws_tmp, auto_rollback=True)
    dl = DecisionLogger()
    docker, client = make_sandbox(dl)
    # 连续两次解析失败 -> 任务 FAILED（max_retries=2）
    llm = ScriptedLLM("not-json-1", "not-json-2")
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(),
                     docker_sandbox=docker, decision_logger=dl)
    try:
        await loop.run("会失败的任务")
    finally:
        await loop.close()
    # 首容器被移除，第二容器从快照镜像重建
    assert len(client.removed) >= 1
    assert len(client._created) == 2
    assert client._created[-1].image.startswith("alphaswe/snap:")
