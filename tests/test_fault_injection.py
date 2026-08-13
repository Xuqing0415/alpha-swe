"""收敛期 P0-1：全链路故障注入测试框架（阶段一 1.1）。

在每个关键注入点上人为注入故障，断言系统按设计降级而非崩溃/卡死；
最后用聚合统计验证整体降级成功率 > 95%。

注入点覆盖（对应方案表格）：
- LLM 调用：超时 / 非法 JSON / 空响应 / LiteLLM 超时与重试
- 工具执行：超时 / 权限拒绝 / 大输出 / 未知工具 / 连续超时熔断
- MCP 连接：握手失败 / 连接期取消 / 异常工具数据 / 循环内坏服务器
- 记忆后端：构造失败 / 检索异常 / 写入异常
- 沙箱：Docker 客户端不可用 / 镜像拉取失败 / 路径穿越 / 受保护文件
- 文件系统：写入 IO 错误 / 编辑参数越界
- 事件总线：订阅者崩溃隔离 / 消息积压

运行：python -X utf8 -m pytest tests/test_fault_injection.py -q
"""
import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from agent.config import (AgentConfig, AppConfig, ContextConfig, LLMConfig,
                          LLMProvider, MCPClientConfig, MCPOptions,
                          MemoryConfig, SandboxConfig)
from agent.core.decision_logger import DecisionLogger
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import LLMServiceError, LiteLLMClient, MockLLM
from agent.memory.factory import build_memory
from agent.memory.store import MemoryStore, NoopMemoryStore
from agent.mcp.manager import MCPManager
from agent.sandbox.docker_sandbox import DockerSandbox
from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import ErrorCategory, ExecutionContext, Tool, ToolResult
from agent.tools.fileio import FileIOTool
from agent.tools.manager import ToolManager
from agent.tools.terminal import TerminalTool

# 目标：故障下成功降级的比例 > 95%
TARGET_DEGRADE_RATE = 0.95


# ---- 测试替身 ----
class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class FailingLLM(MockLLM):
    """总是抛异常的 LLM，模拟服务端/网络故障。"""

    def __init__(self, exc: BaseException):
        self._exc = exc

    async def complete(self, messages):
        raise self._exc


class BrokenMemory(MemoryStore):
    """记忆后端故障替身：检索/写入/去重全部抛异常。"""

    def _raise(self):
        raise RuntimeError("记忆后端故障（模拟锁/存储满/超时）")

    def remember(self, kind, text, metadata=None):
        self._raise()

    def retrieve(self, query, top_k=5):
        self._raise()

    def search(self, query, top_k=5, kinds=None, metadata_filter=None):
        self._raise()

    def find_similar(self, text, top_k=1, kinds=None):
        self._raise()

    def close(self):
        pass


class FakeMCPClient:
    """可编程 MCP 客户端替身（来自阶段六测试）。"""

    def __init__(self, cfg, tool_timeout=30.0, always_fail=False):
        self.config = cfg
        self.connected = False
        self._always_fail = always_fail

    async def connect(self):
        if self._always_fail:
            return False
        self.connected = True
        return True

    async def close(self):
        self.connected = False

    async def list_tools(self):
        return [{"name": "fake_tool", "title": "", "description": "fake",
                 "parameters": {"type": "object", "properties": {}}}]

    async def list_resources(self):
        return []

    async def read_resource(self, uri):
        return ""

    async def call_tool(self, name, arguments):
        return ToolResult(success=True, output="fake-ok")


class CancelOnConnectClient(FakeMCPClient):
    """connect() 抛 CancelledError，模拟 SDK 取消作用域泄漏。"""

    async def connect(self):
        raise asyncio.CancelledError("simulated leaked cancel scope")


class BadToolDataClient(FakeMCPClient):
    """连接成功但工具列表返回异常数据。"""

    async def list_tools(self):
        raise RuntimeError("异常工具数据")


class UnavailableDockerClient:
    """docker SDK 客户端替身：镜像查询与拉取全部失败（守护进程不可达）。"""

    class images:
        @staticmethod
        def get(image):
            raise RuntimeError("daemon unreachable")

        @staticmethod
        def pull(image):
            raise RuntimeError("daemon unreachable")


class IOErrorFileTool(FileIOTool):
    """写入抛 OSError，模拟磁盘满。"""

    async def _write(self, target, content, start, task_id=""):
        raise OSError("模拟磁盘已满")


class SlowTool(Tool):
    name = "slow_tool"
    parameters = {}

    async def execute(self, params, context):
        await asyncio.sleep(30)
        return ToolResult(success=True, output="never")


class TimeoutTool(Tool):
    """每次都返回 timed_out 结果的工具：模拟工具持续超时（无子进程依赖）。"""

    name = "flaky_tool"
    parameters = {}

    async def execute(self, params, context):
        return ToolResult(success=False, error="模拟工具超时",
                          metadata={"timed_out": True},
                          error_category=ErrorCategory.TRANSIENT)


def make_fake_manager(dl=None, always_fail=False, client_cls=FakeMCPClient,
                      ttl=0.0):
    servers = [MCPClientConfig(name="test", transport="stdio", command="x")]
    return MCPManager(
        servers=servers,
        connect_timeout=2.0,
        tool_timeout=5.0,
        reconnect_attempts=2,
        reconnect_delay=0.01,
        resource_cache_ttl=ttl,
        decision_logger=dl,
        client_factory=lambda cfg: client_cls(cfg, tool_timeout=5.0,
                                              always_fail=always_fail),
    )


def make_config(ws_tmp: Path, sandbox_kwargs=None, memory_kwargs=None,
                mcp_kwargs=None, context_kwargs=None) -> AppConfig:
    sandbox_cfg = {"workspace": str(ws_tmp / "ws")}
    sandbox_cfg.update(sandbox_kwargs or {})
    memory_cfg = {"db_path": str(ws_tmp / "mem.db"), "backend": "none",
                  "auto_experience": False}
    memory_cfg.update(memory_kwargs or {})
    mcp_cfg: Dict[str, Any] = {"enabled": False}
    mcp_cfg.update(mcp_kwargs or {})
    context_cfg = {"archive_dir": str(ws_tmp / "logs" / "archives")}
    context_cfg.update(context_kwargs or {})
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1,
                          trace_enabled=False, archive_enabled=False,
                          metrics_enabled=False, snapshot_enabled=False),
        sandbox=SandboxConfig(**sandbox_cfg),
        memory=MemoryConfig(**memory_cfg),
        mcp=MCPOptions(**mcp_cfg),
        context=ContextConfig(**context_cfg),
    )


# ---- 探针：每个返回 (ok, detail)，异常一律降级为 fail 并继续 ----
async def probe_llm_timeout_loop(ws_tmp: Path):
    loop = AgentLoop(config=make_config(ws_tmp),
                     llm=FailingLLM(asyncio.TimeoutError("模拟超时")),
                     planner=StubPlanner())
    try:
        result = await loop.run("LLM 超时故障注入")
        ok = result.phase.name == "FAILED" and "失败" in result.final_answer
        return ok, f"phase={result.phase.name} answer={result.final_answer[:50]!r}"
    finally:
        await loop.close()


async def probe_llm_invalid_json(ws_tmp: Path):
    llm = ScriptedLLM("这不是 JSON", "还不是 JSON")
    loop = AgentLoop(config=make_config(ws_tmp), llm=llm, planner=StubPlanner())
    try:
        result = await loop.run("非法 JSON 故障注入")
        ok = result.phase.name == "FAILED" and "解析失败" in result.final_answer
        return ok, f"phase={result.phase.name} answer={result.final_answer[:60]!r}"
    finally:
        await loop.close()


async def probe_llm_empty_response(ws_tmp: Path):
    llm = ScriptedLLM("", "")
    loop = AgentLoop(config=make_config(ws_tmp), llm=llm, planner=StubPlanner())
    try:
        result = await loop.run("空响应故障注入")
        ok = result.phase.name == "FAILED"
        return ok, f"phase={result.phase.name}"
    finally:
        await loop.close()


async def probe_llm_litellm_timeout(ws_tmp: Path):
    cfg_llm = LLMConfig(provider=LLMProvider.LITELLM, model="gpt-test",
                        timeout=0.05, max_retries=1)
    client = LiteLLMClient(cfg_llm)
    calls = 0

    async def slow(_kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(5)
        return {}

    client._acomplete = slow
    t0 = time.time()
    with pytest.raises(LLMServiceError):
        await client.complete([{"role": "user", "content": "hi"}])
    elapsed = time.time() - t0
    ok = calls == 2 and elapsed < 3.0
    return ok, f"calls={calls} elapsed={elapsed:.2f}s"


async def probe_llm_litellm_retry_recovers(ws_tmp: Path):
    cfg_llm = LLMConfig(provider=LLMProvider.LITELLM, model="gpt-test",
                        timeout=5.0, max_retries=1)
    client = LiteLLMClient(cfg_llm)
    calls = 0

    async def flaky(_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("网络抖动")
        return {"choices": [{"message": {"content": "ok"}}]}

    client._acomplete = flaky
    content = await client.complete([{"role": "user", "content": "hi"}])
    ok = content == "ok" and calls == 2
    return ok, f"content={content!r} calls={calls}"


async def probe_tool_timeout(ws_tmp: Path):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tm = ToolManager(default_timeout=0.1)
    tm.register(SlowTool())
    r = await tm.execute("slow_tool", {}, ctx)
    ok = (not r.success and r.metadata.get("timed_out") is True
          and r.error_category == ErrorCategory.TRANSIENT)
    return ok, f"success={r.success} timed_out={r.metadata.get('timed_out')}"


async def probe_tool_permission(ws_tmp: Path):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = TerminalTool(read_only=True)
    r = await tool.execute({"command": "Remove-Item x.txt"}, ctx)
    ok = (not r.success and r.error_category == ErrorCategory.PERMISSION)
    return ok, f"success={r.success} cat={r.error_category}"


async def probe_tool_large_output(ws_tmp: Path):
    loop = AgentLoop(config=make_config(ws_tmp), llm=MockLLM(),
                     planner=StubPlanner())
    try:
        raw = "\n".join(f"line-{i:04d}" for i in range(1500))
        obs = await loop._summarize_observation(
            "terminal_execute", ToolResult(success=True, output=raw))
        truncated = any(d["name"] == "output.truncated"
                        for d in loop._decision.records())
        archived = (ws_tmp / "logs" / "outputs").is_dir()
        ok = "已压缩" in obs and truncated and archived
        return ok, f"truncated={truncated} archived={archived}"
    finally:
        await loop.close()


async def probe_tool_unknown(ws_tmp: Path):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tm = ToolManager()
    r = await tm.execute("no_such_tool", {}, ctx)
    ok = (not r.success and r.error_category == ErrorCategory.CONFIGURATION)
    return ok, f"success={r.success} cat={r.error_category}"


async def probe_tool_circuit_breaker(ws_tmp: Path):
    tm = ToolManager()
    tm.register(TimeoutTool())
    llm = ScriptedLLM(
        '{"tool": "flaky_tool", "params": {}}',
        '{"tool": "flaky_tool", "params": {}}',
        '{"tool": "flaky_tool", "params": {}}',
    )
    loop = AgentLoop(config=make_config(ws_tmp), llm=llm, planner=StubPlanner(),
                     tools=tm)
    try:
        result = await loop.run("连续超时熔断")
        task = loop.scheduler.dag.get("t0")
        strikes = sum((task.metadata.get("_timeout_strikes") or {}).values())
        ok = (result.ok is False and strikes == 3
              and "熔断" in result.final_answer)
        return ok, f"strikes={strikes} answer={result.final_answer[:60]!r}"
    finally:
        await loop.close()


async def probe_mcp_failed_server(ws_tmp: Path):
    dl = DecisionLogger()
    mgr = make_fake_manager(dl, always_fail=True)
    try:
        ok = await mgr.ensure_connected()
        tools = await mgr.build_tools()
        good = ok == 0 and mgr.failed_servers == ["test"] and tools == []
        return good, f"connected={ok} failed={mgr.failed_servers} tools={len(tools)}"
    finally:
        await mgr.disconnect_all()


async def probe_mcp_cancel_on_connect(ws_tmp: Path):
    mgr = make_fake_manager(client_cls=CancelOnConnectClient)
    try:
        ok = await mgr.ensure_connected()
        good = ok == 0 and mgr.failed_servers == ["test"]
        return good, f"connected={ok} failed={mgr.failed_servers}"
    finally:
        await mgr.disconnect_all()


async def probe_mcp_bad_tool_data(ws_tmp: Path):
    dl = DecisionLogger()
    mgr = make_fake_manager(dl, client_cls=BadToolDataClient)
    try:
        ok = await mgr.ensure_connected()
        tools = await mgr.build_tools()
        good = ok == 1 and tools == []  # 已连接但数据异常 -> 工具隐藏
        return good, f"connected={ok} tools={len(tools)}"
    finally:
        await mgr.disconnect_all()


async def probe_mcp_loop_broken(ws_tmp: Path):
    mgr = make_fake_manager(always_fail=True)
    cfg = make_config(ws_tmp, mcp_kwargs={"enabled": True,
                                          "reconnect_attempts": 1,
                                          "reconnect_delay": 0.01})
    loop = AgentLoop(config=cfg, llm=ScriptedLLM('{"final_answer": "ok"}'),
                     planner=StubPlanner(), mcp_manager=mgr)
    try:
        result = await loop.run("MCP 坏服务器故障注入")
        good = result.ok and mgr.failed_servers == ["test"]
        return good, f"phase={result.phase.name} failed={mgr.failed_servers}"
    finally:
        await loop.close()


async def probe_memory_construct_fail(ws_tmp: Path):
    d = ws_tmp / "memdir"
    d.mkdir(parents=True, exist_ok=True)
    store = build_memory(MemoryConfig(backend="sqlite", db_path=str(d)))
    good = isinstance(store, NoopMemoryStore) and store.disabled
    return good, f"type={type(store).__name__} disabled={getattr(store, 'disabled', False)}"


async def probe_memory_retrieve_fail(ws_tmp: Path):
    loop = AgentLoop(config=make_config(ws_tmp), llm=ScriptedLLM(
        '{"final_answer": "检索故障下仍完成"}'),
        planner=StubPlanner(), memory=BrokenMemory())
    try:
        result = await loop.run("记忆检索故障注入")
        return result.ok, f"phase={result.phase.name}"
    finally:
        await loop.close()


async def probe_memory_write_fail(ws_tmp: Path):
    loop = AgentLoop(config=make_config(ws_tmp), llm=ScriptedLLM(
        '{"final_answer": "写入故障下仍完成"}'),
        planner=StubPlanner(), memory=BrokenMemory())
    try:
        result = await loop.run("记忆写入故障注入")
        return result.ok, f"phase={result.phase.name}"
    finally:
        await loop.close()


async def probe_sandbox_docker_unavailable(ws_tmp: Path):
    cfg = make_config(ws_tmp, sandbox_kwargs={"docker_enabled": True})
    dl = DecisionLogger()
    sb = DockerSandbox(cfg.sandbox, dl)

    def _raise():
        raise RuntimeError("docker SDK 不可用")

    sb._docker = _raise
    loop = AgentLoop(config=cfg, llm=ScriptedLLM('{"final_answer": "ok"}'),
                     planner=StubPlanner(), docker_sandbox=sb)
    try:
        cid = await sb.start(cfg.sandbox.workspace)
        result = await loop.run("Docker 不可用降级")
        degraded = any(d.name == "docker.start_failed"
                       for d in dl.decisions)
        good = cid == "" and result.ok and not sb.running and degraded
        return good, f"cid={cid!r} running={sb.running} degraded={degraded}"
    finally:
        await loop.close()


async def probe_sandbox_docker_image_fail(ws_tmp: Path):
    cfg = make_config(ws_tmp, sandbox_kwargs={"docker_enabled": True})
    dl = DecisionLogger()
    sb = DockerSandbox(cfg.sandbox, dl, client=UnavailableDockerClient())
    try:
        cid = await sb.start(cfg.sandbox.workspace)
        good = cid == "" and not sb.running
        return good, f"cid={cid!r} running={sb.running}"
    finally:
        await sb.stop()


async def probe_sandbox_path_traversal(ws_tmp: Path):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = FileIOTool(workspace=str(ws_tmp))
    r = await tool.execute({"action": "write", "path": r"..\..\evil.txt",
                            "content": "x"}, ctx)
    good = (not r.success and r.error_category == ErrorCategory.PERMISSION)
    return good, f"success={r.success} cat={r.error_category} err={r.error[:40]!r}"


async def probe_sandbox_protected_delete(ws_tmp: Path):
    p = SandboxPolicy(workspace=str(ws_tmp), protected_paths=[".git"])
    ctx = ExecutionContext(workspace=str(ws_tmp))
    allowed, reason = p.check("file_ops",
                              {"action": "delete", "path": ".git"},
                              ctx)
    good = not allowed and "受保护" in reason
    return good, f"allowed={allowed} reason={reason[:40]!r}"


async def probe_fs_write_io_error(ws_tmp: Path):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = IOErrorFileTool(workspace=str(ws_tmp))
    r = await tool.execute({"action": "write", "path": "a.txt",
                            "content": "x"}, ctx)
    good = not r.success and "磁盘已满" in r.error
    return good, f"success={r.success} err={r.error[:40]!r}"


async def probe_fs_edit_invalid_range(ws_tmp: Path):
    (ws_tmp / "doc.txt").write_text("a\nb\nc\n", encoding="utf-8")
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = FileIOTool(workspace=str(ws_tmp))
    r = await tool.execute({"action": "edit", "path": "doc.txt",
                            "start_line": 10, "end_line": 12,
                            "content": "x"}, ctx)
    good = (not r.success and r.error_category == ErrorCategory.PERMANENT)
    return good, f"success={r.success} err={r.error[:40]!r}"


async def probe_bus_subscriber_crash(ws_tmp: Path):
    loop = AgentLoop(config=make_config(ws_tmp), llm=MockLLM(),
                     planner=StubPlanner())

    def crash(_record):
        raise RuntimeError("订阅者 bug")

    got: List[Dict[str, Any]] = []

    def healthy(record):
        got.append(record)

    loop.subscribe(crash)
    loop.subscribe(healthy)
    for i in range(5):
        loop._emit("test_event", i=i)
    await loop.close()
    good = len(got) == 5
    return good, f"healthy_received={len(got)}"


async def probe_bus_backlog(ws_tmp: Path):
    loop = AgentLoop(config=make_config(ws_tmp), llm=MockLLM(),
                     planner=StubPlanner())
    got: List[Dict[str, Any]] = []

    def collect(record):
        got.append(record)

    loop.subscribe(collect)
    for i in range(200):
        loop._emit("backlog", i=i)
    await loop.close()
    good = len(got) == 200
    return good, f"received={len(got)}/200"


# ---- 探针注册表 ----
@dataclass
class Probe:
    point: str
    name: str
    fn: Callable[[Path], Any]


PROBES: List[Probe] = [
    Probe("llm", "timeout_loop", probe_llm_timeout_loop),
    Probe("llm", "invalid_json_loop", probe_llm_invalid_json),
    Probe("llm", "empty_response_loop", probe_llm_empty_response),
    Probe("llm", "litellm_timeout_bounded", probe_llm_litellm_timeout),
    Probe("llm", "litellm_retry_recovers", probe_llm_litellm_retry_recovers),
    Probe("tool", "timeout_classified", probe_tool_timeout),
    Probe("tool", "permission_readonly", probe_tool_permission),
    Probe("tool", "large_output_truncated", probe_tool_large_output),
    Probe("tool", "unknown_name_graceful", probe_tool_unknown),
    Probe("tool", "circuit_breaker_three_strikes", probe_tool_circuit_breaker),
    Probe("mcp", "failed_server_degrades_tools", probe_mcp_failed_server),
    Probe("mcp", "cancel_on_connect_degrades", probe_mcp_cancel_on_connect),
    Probe("mcp", "bad_tool_data_hidden", probe_mcp_bad_tool_data),
    Probe("mcp", "loop_with_broken_server", probe_mcp_loop_broken),
    Probe("memory", "construct_fail_noop", probe_memory_construct_fail),
    Probe("memory", "retrieve_fail_loop_ok", probe_memory_retrieve_fail),
    Probe("memory", "write_fail_loop_ok", probe_memory_write_fail),
    Probe("sandbox", "docker_client_unavailable", probe_sandbox_docker_unavailable),
    Probe("sandbox", "docker_image_pull_fail", probe_sandbox_docker_image_fail),
    Probe("sandbox", "path_traversal_blocked", probe_sandbox_path_traversal),
    Probe("sandbox", "protected_delete_blocked", probe_sandbox_protected_delete),
    Probe("fs", "write_io_error", probe_fs_write_io_error),
    Probe("fs", "edit_invalid_range", probe_fs_edit_invalid_range),
    Probe("bus", "subscriber_crash_isolated", probe_bus_subscriber_crash),
    Probe("bus", "backlog_200_events", probe_bus_backlog),
]


@pytest.mark.asyncio
async def test_fault_injection_degradation_rate(ws_tmp):
    """聚合统计：全部注入点的降级成功率必须 > 95%。"""
    results = []
    for probe in PROBES:
        try:
            ok, detail = await probe.fn(ws_tmp)
        except Exception as e:  # 探针自身异常也计为降级失败
            ok, detail = False, f"探针异常 {type(e).__name__}: {e}"
        results.append((probe.point, probe.name, ok, detail))

    passed = sum(1 for _, _, ok, _ in results if ok)
    total = len(results)
    rate = passed / total
    print(f"\n[故障注入] 降级成功率: {passed}/{total} = {rate:.1%}")
    for point, name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {point}.{name}: {detail}")
    assert rate > TARGET_DEGRADE_RATE, (
        f"降级成功率 {rate:.1%} 未达到目标 >{TARGET_DEGRADE_RATE:.0%}；"
        f"失败: {[f'{p}.{n}' for p, n, ok, _ in results if not ok]}"
    )