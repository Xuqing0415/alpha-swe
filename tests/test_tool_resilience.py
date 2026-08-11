"""执行引擎可靠性测试（方案 2.1/4.1）：错误分类、超时安全网、连续超时熔断。"""
import asyncio
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.tools.base import ErrorCategory, ExecutionContext, ToolResult
from agent.tools.manager import ToolManager
from agent.tools.terminal import TerminalTool


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp: Path):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


def test_error_category_default_and_to_dict():
    r = ToolResult(success=False, error="boom")
    assert r.error_category == ErrorCategory.UNKNOWN
    assert r.to_dict()["error_category"] == "unknown"


@pytest.mark.asyncio
async def test_terminal_timeout_classified_transient(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = TerminalTool(default_timeout=1.0)
    r = await tool.execute({"command": "Start-Sleep -Seconds 5", "timeout": 1}, ctx)
    assert r.success is False
    assert r.metadata.get("timed_out") is True
    assert r.error_category == ErrorCategory.TRANSIENT


@pytest.mark.asyncio
async def test_terminal_readonly_classified_permission(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = TerminalTool(read_only=True)
    r = await tool.execute({"command": "Remove-Item x.txt"}, ctx)
    assert r.success is False
    assert r.error_category == ErrorCategory.PERMISSION


@pytest.mark.asyncio
async def test_manager_wait_for_timeout_returns_transient(ws_tmp):
    from agent.tools.base import Tool

    class SlowTool(Tool):
        name = "slow_tool"
        parameters = {}

        async def execute(self, params, context):
            await asyncio.sleep(30)
            return ToolResult(success=True, output="never")

    ctx = ExecutionContext(workspace=str(ws_tmp))
    tm = ToolManager(default_timeout=0.1)
    tm.register(SlowTool())
    r = await tm.execute("slow_tool", {}, ctx)
    assert r.success is False
    assert r.metadata.get("timed_out") is True
    assert r.error_category == ErrorCategory.TRANSIENT


@pytest.mark.asyncio
async def test_loop_circuit_breaker_after_three_timeouts(ws_tmp):
    """同一命令连续超时 3 次触发熔断，任务被标记 FAILED 而非无限重试。"""
    cfg = make_config(ws_tmp)
    # 每次都用同样的慢命令，让 TerminalTool 自管超时（0.5s）快速失败
    llm = ScriptedLLM(
        '{"tool": "terminal_execute", "params": {"command": "Start-Sleep -Seconds 5", "timeout": 0.5}}',
        '{"tool": "terminal_execute", "params": {"command": "Start-Sleep -Seconds 5", "timeout": 0.5}}',
        '{"tool": "terminal_execute", "params": {"command": "Start-Sleep -Seconds 5", "timeout": 0.5}}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("连续超时熔断测试")
    assert result.ok is False
    assert "熔断" in result.final_answer
    task = loop.scheduler.dag.get("t0")
    assert task.metadata.get("_timeout_strikes", {}).get(
        "terminal:Start-Sleep -Seconds 5") == 3
