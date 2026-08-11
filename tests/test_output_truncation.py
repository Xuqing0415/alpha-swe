"""输出截断三级处理测试（方案 2.2）：全文 / 头尾+存档 / 关键行+LLM 摘要。"""
from pathlib import Path

import pytest

from agent.config import (ContextConfig, MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.tools.base import ToolResult


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


def make_config(ws_tmp: Path):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
        context=ContextConfig(archive_dir=str(ws_tmp / "logs" / "archives")),
    )


@pytest.mark.asyncio
async def test_small_output_kept_full(ws_tmp):
    loop = AgentLoop(config=make_config(ws_tmp), llm=MockLLM(),
                     planner=StubPlanner())
    r = ToolResult(success=True, output="短输出")
    obs = await loop._summarize_observation("terminal_execute", r)
    assert obs == "[terminal_execute] 短输出"
    assert "已压缩" not in obs


@pytest.mark.asyncio
async def test_medium_output_head_tail_and_archive(ws_tmp):
    loop = AgentLoop(config=make_config(ws_tmp), llm=MockLLM(),
                     planner=StubPlanner())
    raw = "\n".join(f"line-{i:04d}" for i in range(1500))  # ~15k 字符
    r = ToolResult(success=True, output=raw)
    obs = await loop._summarize_observation("terminal_execute", r)
    assert "已压缩" in obs
    assert "开头: [terminal_execute] line-0000" in obs
    assert "line-1499" in obs  # 尾部保留
    # 存档文件存在且包含完整输出
    outputs = ws_tmp / "logs" / "outputs"
    assert outputs.is_dir()
    archived = list(outputs.glob("*.txt"))
    assert len(archived) == 1
    assert "line-1499" in archived[0].read_text(encoding="utf-8")
    # 决策日志记录了压缩
    assert any(r_["name"] == "output.truncated"
               for r_ in loop._decision.records())


@pytest.mark.asyncio
async def test_huge_output_key_lines_via_mock(ws_tmp):
    """>20k 且 LLM 为 Mock 时退化为关键行提取，不消耗脚本化响应。"""
    loop = AgentLoop(config=make_config(ws_tmp), llm=MockLLM(),
                     planner=StubPlanner())
    raw = "\n".join(
        [f"info-{i:04d}" for i in range(5000)]
        + ["ERROR: build failed at src/main.py:42", "WARN: deprecated api"]
    )
    r = ToolResult(success=True, output=raw)
    obs = await loop._summarize_observation("terminal_execute", r)
    assert "ERROR: build failed" in obs or "ERROR: build failed" in obs
    assert "关键行" in obs
    outputs = ws_tmp / "logs" / "outputs"
    assert len(list(outputs.glob("*.txt"))) >= 1
