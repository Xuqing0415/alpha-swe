"""阶段五测试：沙箱安全加固（网络细粒度策略/假网络、文件保护、审计回滚、资源熔断）。"""
import asyncio

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.sandbox.audit import FileAuditStore
from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import ExecutionContext
from agent.tools.fileio import FileIOTool
from agent.tools.manager import ToolManager
from agent.tools.terminal import TerminalTool


def policy(**kw):
    return SandboxPolicy(
        workspace=".",
        protected_paths=[".git", "config/agent.yaml"],
        **kw,
    )


# ---- 1. 危险命令 ----

def test_policy_blocks_dangerous_commands():
    p = policy()
    ctx = ExecutionContext(workspace=".")
    for cmd in ["sudo apt install x", "rm -rf /", "mkfs.ext4 /dev/sda", "dd if=/dev/zero of=/dev/sda"]:
        ok, reason = p.check("terminal_execute", {"command": cmd}, ctx)
        assert not ok, cmd


# ---- 2. 网络细粒度策略 ----

def test_network_deny_blocks_curl():
    from agent.core.decision_logger import DecisionLogger
    dl = DecisionLogger()
    p = policy(network_enabled=False, network_policy="deny", decision_logger=dl)
    ctx = ExecutionContext(workspace=".")
    ok, reason = p.check("terminal_execute", {"command": "curl https://evil.example.com/x"}, ctx)
    assert not ok and "网络已禁用" in reason
    assert any(dp.name == "block_network_command" for dp in dl.decisions)


def test_network_allowlist_pip_allowed_curl_blocked():
    p = policy(network_policy="allowlist",
               network_allowed_commands=["pip install", "apt-get"])
    ctx = ExecutionContext(workspace=".")
    ok, _ = p.check("terminal_execute", {"command": "pip install requests"}, ctx)
    assert ok
    ok2, _ = p.check("terminal_execute", {"command": "apt-get update"}, ctx)
    assert ok2
    ok3, reason = p.check("terminal_execute", {"command": "curl https://x.com"}, ctx)
    assert not ok3 and "网络已禁用" in reason


def test_network_audit_records_url():
    from agent.core.decision_logger import DecisionLogger
    dl = DecisionLogger()
    p = policy(network_policy="deny", decision_logger=dl)
    ctx = ExecutionContext(workspace=".")
    p.check("terminal_execute",
            {"command": "curl https://api.github.com/repos/x/y"}, ctx)
    audited = [dp for dp in dl.decisions if dp.name == "network.audit"]
    assert audited and "https://api.github.com/repos/x/y" in audited[0].decision


def test_fake_network_returns_preset_without_subprocess():
    p = policy(network_enabled=True, fake_network=True,
               fake_network_responses={"https://api.example.com": '{"ok": true}'})
    tm = ToolManager(policy=p)
    tm.register(TerminalTool())
    result = asyncio.run(tm.execute(
        "terminal_execute", {"command": "curl https://api.example.com/users"},
        ExecutionContext(workspace=".")))
    assert result.success
    assert result.metadata.get("fake_network") is True
    assert '{"ok": true}' in result.output
    # 未命中预设的 URL 不拦截（交由策略层或真实执行）
    assert p.intercept("terminal_execute",
                       {"command": "curl https://other.example.com"}) is None


# ---- 3. 文件系统保护 ----

def test_protected_path_delete_blocked():
    from agent.core.decision_logger import DecisionLogger
    dl = DecisionLogger()
    p = policy(decision_logger=dl)
    ctx = ExecutionContext(workspace=".")
    for cmd in ["rm -rf .git", "rm -r .git", "Remove-Item -Recurse .git",
                "del .git\\config", "rm -rf config"]:
        ok, reason = p.check("terminal_execute", {"command": cmd}, ctx)
        assert not ok and "受保护" in reason, cmd
    assert any(dp.name == "file.protect" for dp in dl.decisions)
    # 普通文件删除放行（策略层不拦，交给删除确认/审计）
    ok, _ = p.check("terminal_execute", {"command": "rm -f tmp.txt"}, ctx)
    assert ok


def test_protected_write_blocked():
    p = policy()
    ctx = ExecutionContext(workspace=".")
    ok, reason = p.check("file_ops",
                         {"action": "write", "path": "config/agent.yaml", "content": "x"}, ctx)
    assert not ok and "受保护" in reason


def test_path_traversal_and_nul_blocked():
    p = policy()
    ctx = ExecutionContext(workspace=".")
    ok, _ = p.check("file_ops", {"action": "write", "path": "../../etc/passwd", "content": "x"}, ctx)
    assert not ok
    ok2, _ = p.check("file_ops", {"action": "write", "path": "a\x00b.txt", "content": "x"}, ctx)
    assert not ok2


# ---- 4. 文件操作审计与回滚 ----

@pytest.mark.asyncio
async def test_file_audit_records_diff_and_rollback(ws_tmp):
    ws = ws_tmp / "ws"
    ws.mkdir()
    audit = FileAuditStore(str(ws_tmp / "audit"))
    tool = FileIOTool(audit_store=audit)
    ctx = ExecutionContext(workspace=str(ws), task_id="t1")
    await tool.execute({"action": "write", "path": "a.txt", "content": "line1\n"}, ctx)
    await tool.execute({"action": "write", "path": "a.txt", "content": "line1\nline2\n"}, ctx)
    rows = audit.find(str(ws / "a.txt"))
    assert len(rows) == 2
    assert rows[0]["before"] == "line1\n"
    assert rows[0]["after"] == "line1\nline2\n"
    assert "+line2" in rows[0]["diff"]
    restored = audit.rollback(str(ws / "a.txt"))
    assert restored == "line1\n"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "line1\n"


# ---- 5. 资源监控与熔断 ----

@pytest.mark.asyncio
async def test_circuit_breaker_kills_memory_hog():
    pytest.importorskip("psutil")
    tool = TerminalTool(resource_monitor=True, memory_limit_mb=150,
                        poll_interval=0.05)
    result = await tool.execute(
        {"command": "python -c \"x=[bytearray(1024*1024) for _ in range(300)];"
                    " import time; time.sleep(30)\""},
        ExecutionContext(workspace="."))
    assert result.success is False
    assert result.metadata.get("circuit_breaker") is True
    assert "熔断" in (result.error or "")


@pytest.mark.asyncio
async def test_circuit_breaker_allows_normal_command():
    pytest.importorskip("psutil")
    tool = TerminalTool(resource_monitor=True, memory_limit_mb=150,
                        poll_interval=0.05)
    result = await tool.execute({"command": "echo ok"},
                                ExecutionContext(workspace="."))
    assert result.success
    assert result.metadata.get("circuit_breaker") is None


# ---- 6. 端到端：循环内恶意命令被拦截 ----

class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_loop_blocks_network_and_protected_commands(ws_tmp):
    cfg = AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2),
        sandbox=SandboxConfig(
            workspace=str(ws_tmp / "ws"),
            no_network=True,
            protected_paths=[".git"],
            audit_dir=str(ws_tmp / "audit"),
        ),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )
    llm = ScriptedLLM(
        '{"tool": "terminal_execute", "params": {"command": "curl https://evil.com"}}',
        '{"tool": "terminal_execute", "params": {"command": "rm -rf .git"}}',
        '{"tool": "file_ops", "params": {"action": "write", "path": "ok.txt", "content": "hi"}}',
        '{"final_answer": "完成"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("安全测试")
    assert result.ok
    task = loop.scheduler.dag.get("t0")
    obs = [h["content"] for h in task.history if h["role"] == "observation"]
    assert any("网络已禁用" in o for o in obs)
    assert any("受保护路径" in o for o in obs)
    assert any("写入成功" in o for o in obs)
    # 审计记录了普通文件写入
    audit = FileAuditStore(str(ws_tmp / "audit"))
    assert audit.find(str(ws_tmp / "ws" / "ok.txt"))