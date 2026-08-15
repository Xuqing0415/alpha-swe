# -*- coding: utf-8 -*-
"""进阶 2.3 资源预算管理测试：token/时间预算、告警、借用与耗尽处理。

覆盖：
- Task 预算字段序列化；
- Planner 按复杂度估算预算；
- 任务级 token 累计（_add_task_tokens）；
- 80% 告警事件与决策点；
- 预算耗尽 -> 任务 FAILED 且不重试；
- 高优先级借用低优先级未用预算；
- 时间预算耗尽；
- _retry_available 对预算耗尽任务返回 False。
"""
import time
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop, TaskBudgetExceeded
from agent.core.task import Task, TaskDAG, TaskStatus
from agent.llm import MockLLM


class StubPlanner:
    def __init__(self, task_id: str = "t0"):
        self.task_id = task_id

    async def plan(self, prompt, context=""):
        return [Task(id=self.task_id, instruction=prompt,
                     criticality="critical")]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp: Path):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"),
                            auto_experience=False),
        mcp=MCPOptions(enabled=False),
    )


def test_budget_fields_serialization():
    dag = TaskDAG()
    t = Task(id="t", instruction="x", token_budget=5000, time_budget=120.0)
    dag.add(t)
    restored = TaskDAG.from_snapshot(dag.to_snapshot())
    rt = restored.get("t")
    assert rt.token_budget == 5000
    assert rt.time_budget == 120.0


def test_add_task_tokens(ws_tmp):
    cfg = make_config(ws_tmp)
    loop = AgentLoop(config=cfg, llm=MockLLM(), planner=StubPlanner())
    t = Task(id="t", instruction="x")
    loop.scheduler.submit(t)
    loop._add_task_tokens(t, 100)
    loop._add_task_tokens(t, 50)
    assert t.metadata["_tokens_used"] == 150
    loop._add_task_tokens(None, 999)  # 无任务时不报错
    assert t.metadata["_tokens_used"] == 150


def test_budget_warning_emitted(ws_tmp):
    cfg = make_config(ws_tmp)
    loop = AgentLoop(config=cfg, llm=MockLLM(), planner=StubPlanner())
    t = Task(id="t0", instruction="x", token_budget=1000, time_budget=1000)
    loop.scheduler.submit(t)
    t.metadata["_tokens_used"] = 900  # >= 80% 告警阈值
    loop._maybe_enforce_budget(t)
    assert any(
        e["type"] == "budget_warning" and e["data"]["kind"] == "token"
        for e in loop.events
    )
    # 超过 100% -> 借用无来源 -> 抛预算耗尽
    t.metadata["_tokens_used"] = 2000
    with pytest.raises(TaskBudgetExceeded) as ei:
        loop._maybe_enforce_budget(t)
    assert ei.value.kind == "token"
    assert ei.value.report


def test_time_budget_exhaustion(ws_tmp):
    cfg = make_config(ws_tmp)
    loop = AgentLoop(config=cfg, llm=MockLLM(), planner=StubPlanner())
    t = Task(id="t0", instruction="x", token_budget=100000, time_budget=5)
    loop.scheduler.submit(t)
    t.metadata["_started_at"] = time.monotonic() - 10
    with pytest.raises(TaskBudgetExceeded) as ei:
        loop._maybe_enforce_budget(t)
    assert ei.value.kind == "time"


def test_borrow_budget_from_lower_priority(ws_tmp):
    cfg = make_config(ws_tmp)
    loop = AgentLoop(config=cfg, llm=MockLLM(), planner=StubPlanner())
    high = Task(id="high", instruction="h", priority=10, token_budget=100)
    low = Task(id="low", instruction="l", priority=0, token_budget=10000)
    loop.scheduler.submit(high)
    loop.scheduler.submit(low)
    high.metadata["_tokens_used"] = 500
    got = loop._borrow_budget(high, 400)
    assert got == 400
    assert low.metadata["_budget_lent"] == 400
    assert high.metadata["_budget_borrowed"] == 400
    # 借用后 500 <= 100 + 400，不再触发耗尽
    loop._maybe_enforce_budget(high)
    assert not any(e["type"] == "budget_exhausted" for e in loop.events)


def test_retry_available_false_when_budget_exhausted():
    t = Task(id="t", instruction="x", max_retries=3)
    t.metadata["_budget_exhausted"] = True
    assert AgentLoop._retry_available(t) is False


@pytest.mark.asyncio
async def test_budget_exhaustion_fails_task_no_retry(ws_tmp):
    """token 预算极小：一次 LLM 调用即超限 -> 任务 FAILED 且不重试。"""

    class BudgetPlanner:
        async def plan(self, prompt, context=""):
            return [Task(id="t0", instruction=prompt, token_budget=1,
                         time_budget=3600, criticality="critical")]

    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"tool": "file_ops", "params": {"action": "read", "path": "nope.py"}}'
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=BudgetPlanner())
    result = await loop.run("预算测试")
    assert result.ok is False
    t = loop.scheduler.dag.get("t0")
    assert t.status == TaskStatus.FAILED
    assert "预算耗尽" in (t.error or "")
    assert t.retry_count == 0  # 预算耗尽不进入重试循环
    assert any(e["type"] == "budget_exhausted" for e in loop.events)
    assert t.metadata["budget_report"]