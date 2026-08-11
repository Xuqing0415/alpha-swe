"""步骤级降级与跳过测试（方案 1.2）：criticality + SKIPPED。"""
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task, TaskStatus
from agent.llm import MockLLM


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class SinglePlanner:
    def __init__(self, criticality="normal", max_retries=0):
        self.criticality = criticality
        self.max_retries = max_retries

    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=self.max_retries,
                     criticality=self.criticality)]


class TwoTaskPlanner:
    """a 会失败（normal，跳过），b 依赖 a，验证跳过不阻塞后续步骤。"""

    async def plan(self, prompt, context=""):
        a = Task(id="a", instruction="失败但可跳过的步骤", max_retries=0,
                 criticality="normal")
        b = Task(id="b", instruction="后续步骤", dependencies=["a"],
                 max_retries=0, criticality="critical")
        return [a, b]


def make_config(ws_tmp: Path):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


@pytest.mark.asyncio
async def test_normal_step_skipped_not_failing_run(ws_tmp):
    """normal 步骤失败且无重试预算 -> SKIPPED，会话仍算完成。"""
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"hello": 1}', '{"world": 2}')
    loop = AgentLoop(config=cfg, llm=llm,
                     planner=SinglePlanner(criticality="normal", max_retries=0))
    result = await loop.run("可跳过步骤测试")
    assert result.ok
    task = loop.scheduler.dag.get("t0")
    assert task.status == TaskStatus.SKIPPED
    assert "[已跳过步骤]" in result.final_answer
    assert "可跳过步骤测试" in result.final_answer


@pytest.mark.asyncio
async def test_critical_step_failure_fails_run(ws_tmp):
    """critical 步骤失败 -> FAILED，会话整体失败。"""
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"hello": 1}', '{"world": 2}')
    loop = AgentLoop(config=cfg, llm=llm,
                     planner=SinglePlanner(criticality="critical", max_retries=0))
    result = await loop.run("关键步骤测试")
    assert result.ok is False
    assert loop.scheduler.dag.get("t0").status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_skipped_dependency_does_not_block_dependents(ws_tmp):
    """a 被跳过（normal）后，依赖它的 b 仍被提升并成功执行。"""
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"hello": 1}',           # a 第一次解析失败
        '{"world": 2}',           # a 第二次解析失败 -> SKIPPED
        '{"final_answer": "b 完成"}',  # b 成功
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=TwoTaskPlanner())
    result = await loop.run("跳过依赖测试")
    assert result.ok
    assert "b 完成" in result.final_answer
    assert loop.scheduler.dag.get("a").status == TaskStatus.SKIPPED
    assert loop.scheduler.dag.get("b").status == TaskStatus.COMPLETED
