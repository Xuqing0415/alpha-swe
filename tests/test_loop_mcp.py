"""AgentLoop 与 MCP 集成测试：工具合并、LLM 调用 MCP 工具、资源注入。"""
import sys
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, MCPClientConfig,
                          MemoryConfig, MCPOptions, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.mcp.manager import MCPManager

SERVER = str(Path(__file__).parent / "mcp_test_server.py")


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        # MockLLM 是 dataclass，必须初始化其字段（calls/responder）
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp, **mcp_kw):
    mcp_kw.setdefault("enabled", True)
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(connect_timeout=10.0,
                       tool_timeout=10.0, **mcp_kw),
    )


def make_manager():
    return MCPManager(
        servers=[MCPClientConfig(name="test", transport="stdio",
                                 command=sys.executable, args=[SERVER])],
        connect_timeout=10.0, tool_timeout=10.0,
    )


@pytest.mark.asyncio
async def test_mcp_tool_merged_and_callable(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"tool": "add", "params": {"a": 2, "b": 3}}',
        '{"final_answer": "结果已计算"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(),
                     mcp_manager=make_manager())
    try:
        result = await loop.run("计算 2 加 3")
        assert result.ok
        # MCP 工具已合并进 ToolManager
        assert loop.tools.get("add") is not None
        assert loop.tools.get("add").description.endswith("(via MCP server: test)")
        # 工具 Schema 出现在系统 Prompt 中
        system_prompt = llm.calls[0][0]["content"]
        assert "add" in system_prompt
    finally:
        await loop.close()


@pytest.mark.asyncio
async def test_mcp_resource_injected_into_prompt(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "完成"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(),
                     mcp_manager=make_manager())
    try:
        await loop.run("测试知识")
        assert "pytest" in loop.prompt_builder.resources_context
    finally:
        await loop.close()


@pytest.mark.asyncio
async def test_mcp_disabled_skips_connection(ws_tmp):
    cfg = make_config(ws_tmp, enabled=False)
    llm = ScriptedLLM('{"final_answer": "无 MCP"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(),
                     mcp_manager=make_manager())
    try:
        result = await loop.run("不连接 MCP")
        assert result.ok
        assert loop.tools.get("add") is None
        assert loop.prompt_builder.resources_context == ""
    finally:
        await loop.close()


@pytest.mark.asyncio
async def test_mcp_failure_does_not_break_run(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "即使 MCP 挂了也完成"}')
    bad_manager = MCPManager(
        servers=[MCPClientConfig(name="bad", transport="stdio",
                                 command="no-such-cmd")],
        connect_timeout=5.0, tool_timeout=5.0,
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(),
                     mcp_manager=bad_manager)
    try:
        result = await loop.run("MCP 挂掉的任务")
        assert result.ok
        assert loop.tools.get("add") is None
    finally:
        await loop.close()
