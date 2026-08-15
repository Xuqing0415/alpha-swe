"""任务级重试测试（方案 1.1）：immediate / retry_with_context / 预算耗尽。"""
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task, TaskStatus
from agent.llm import MockLLM


class StubPlanner:
    def __init__(self, max_retries: int = 3, strategy: str = "immediate",
                 task_id: str = "t0"):
        self.max_retries = max_retries
        self.strategy = strategy
        self.task_id = task_id

    async def plan(self, prompt, context=""):
        # 重试语义面向关键步骤：预算耗尽应保持 FAILED 而非降级跳过
        return [Task(id=self.task_id, instruction=prompt,
                     max_retries=self.max_retries,
                     retry_strategy=self.strategy,
                     criticality="critical")]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp: Path):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1,
                         budget_enabled=False),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


def test_task_retry_defaults_and_serialization():
    t = Task(id="t", instruction="x")
    assert t.max_retries == 3
    assert t.retry_count == 0
    assert t.retry_strategy == "backoff"
    assert t.criticality == "critical"
    d = t.to_dict()
    assert d["max_retries"] == 3 and d["retry_strategy"] == "backoff"
    assert TaskStatus.RETRYING.value == "retrying"


def test_retry_delay_by_strategy():
    t = Task(id="t", instruction="x", retry_strategy="backoff", retry_count=1)
    assert AgentLoop._retry_delay(t) == 1.0
    t.retry_count = 2
    assert AgentLoop._retry_delay(t) == 2.0
    t.retry_strategy = "immediate"
    assert AgentLoop._retry_delay(t) == 0.0
    t.retry_strategy = "retry_with_context"
    assert AgentLoop._retry_delay(t) == 0.0


@pytest.mark.asyncio
async def test_loop_retry_succeeds_after_initial_failure(ws_tmp):
    """首次执行解析失败 -> 任务级重试 -> 第二次成功。"""
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"hello": 1}',          # 第一次执行：解析失败
        '{"world": 2}',          # 第一次执行：解析失败耗尽 -> 任务 FAILED
        '{"final_answer": "重试成功"}',  # 第二次执行：完成
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(max_retries=3))
    result = await loop.run("重试测试")
    assert result.ok
    assert result.final_answer == "重试成功"
    task = loop.scheduler.dag.get("t0")
    assert task.retry_count == 1
    assert task.status == TaskStatus.COMPLETED
    assert any(r.get("name") == "task.retry"
               for r in loop._decision.records())


@pytest.mark.asyncio
async def test_loop_retry_exhausted_stays_failed(ws_tmp):
    """重试预算耗尽后任务保持 FAILED，且反例记忆只在耗尽时写入。"""
    cfg = make_config(ws_tmp)
    # parser.max_retries=2，每次执行消耗 2 个响应；预算 3 -> 共 4 次执行
    llm = ScriptedLLM(
        *(['{"a": 1}', '{"b": 2}'] * 4)
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(max_retries=3))
    result = await loop.run("重试耗尽测试")
    assert result.ok is False
    task = loop.scheduler.dag.get("t0")
    assert task.status == TaskStatus.FAILED
    assert task.retry_count == 3
    assert "解析失败" in result.final_answer


@pytest.mark.asyncio
async def test_loop_retry_with_context_injects_failure_reason(ws_tmp):
    """retry_with_context 策略应把上一步失败原因注入下一轮 Prompt。"""
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"hello": 1}',
        '{"world": 2}',
        '{"final_answer": "修正后完成"}',
    )
    loop = AgentLoop(config=cfg, llm=llm,
                     planner=StubPlanner(max_retries=3,
                                         strategy="retry_with_context"))
    result = await loop.run("带上下文的失败重试")
    assert result.ok
    task = loop.scheduler.dag.get("t0")
    assert task.retry_count == 1
    assert any("[上一步失败原因]" in h.get("content", "")
               for h in task.history)


@pytest.mark.asyncio
async def test_loop_retry_respects_zero_budget(ws_tmp):
    """max_retries=0 时失败不重试，保持原有一次性语义。"""
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"hello": 1}', '{"world": 2}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(max_retries=0))
    result = await loop.run("无重试预算")
    assert result.ok is False
    task = loop.scheduler.dag.get("t0")
    assert task.retry_count == 0
    assert task.status == TaskStatus.FAILED
