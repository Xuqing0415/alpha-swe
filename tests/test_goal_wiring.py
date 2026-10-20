# -*- coding: utf-8 -*-
"""Goal（目标驱动）接线测试：Prompt 注入 / 任务 DAG / 进度回写 / 失败收敛。"""
import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.goal import GoalStatus
from agent.core.loop import AgentLoop
from agent.llm import MockLLM


class ScriptedLLM(MockLLM):
    """按脚本返回响应并记录每次调用消息。"""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        if not self._responses:
            return '{"final_answer": "完成"}'
        return self._responses.pop(0)


def _config(ws, goal_enabled=True):
    return AppConfig(
        agent=AgentConfig(
            max_rounds=2, max_retries=0, max_concurrency=1,
            goal_enabled=goal_enabled,
            goal_state_path=".swe-agent/goals.json",
            goal_idle_continue_limit=2,
            self_improve_enabled=False,
            state_tracker_enabled=False,
            workspace_context_enabled=False,
            snapshot_enabled=False,
        ),
        sandbox=SandboxConfig(workspace=str(ws), docker_enabled=False),
        memory=MemoryConfig(backend="sqlite", db_path=str(ws / "mem.db"),
                            auto_experience=False),
        mcp=MCPOptions(enabled=False),
    )


@pytest.mark.asyncio
async def test_goal_injected_into_prompt_and_completed(ws_tmp):
    """单目标：直接用目标任务执行，Prompt 含目标块，完成后台账落盘。"""
    llm = ScriptedLLM('{"final_answer": "登录接口已实现"}')
    loop = AgentLoop(config=_config(ws_tmp), llm=llm)
    goal = loop.create_goal("实现登录接口")
    try:
        result = await loop.run("实现登录接口")
        assert result.ok
        assert result.tasks and result.tasks[0].id == goal.id
        assert "[当前目标]" in llm.calls[0][0]["content"]
        stored = loop.goal_store.get(goal.id)
        assert stored.status is GoalStatus.COMPLETED
        assert stored.progress == 1.0
        assert (ws_tmp / ".swe-agent" / "goals.json").exists()
    finally:
        await loop.close()


@pytest.mark.asyncio
async def test_goal_subtasks_and_progress_rollup(ws_tmp):
    """子目标按顺序生成任务，全部完成后父目标进度归一到 100%。"""
    llm = ScriptedLLM('{"final_answer": "代码完成"}',
                      '{"final_answer": "测试完成"}')
    loop = AgentLoop(config=_config(ws_tmp), llm=llm)
    goal = loop.create_goal("交付登录功能", ["实现登录接口", "补充回归测试"])
    try:
        result = await loop.run("交付登录功能")
        assert result.ok
        assert len(result.tasks) == 2
        assert [t.id for t in result.tasks] == [g.id for g in goal.sub_goals]
        stored = loop.goal_store.get(goal.id)
        assert stored.status is GoalStatus.COMPLETED
        assert stored.progress == 1.0
        assert all(g.status is GoalStatus.COMPLETED for g in stored.sub_goals)
    finally:
        await loop.close()


@pytest.mark.asyncio
async def test_goal_failure_marks_subgoal_and_parent_failed(ws_tmp):
    """子任务重试耗尽后，子目标与父目标均收敛为 failed。"""
    llm = ScriptedLLM("不是合法输出", "仍然不是合法输出")
    loop = AgentLoop(config=_config(ws_tmp), llm=llm)
    goal = loop.create_goal("交付失败演示", ["无法完成的任务"])
    try:
        result = await loop.run("交付失败演示")
        assert not result.ok
        stored = loop.goal_store.get(goal.id)
        assert stored.sub_goals[0].status is GoalStatus.FAILED
        assert stored.status is GoalStatus.FAILED
    finally:
        await loop.close()


@pytest.mark.asyncio
async def test_goal_idle_continue_executes_remaining_tasks(ws_tmp):
    """队列空闲时按剩余子目标续规划，并在完成后收敛父目标。"""
    llm = ScriptedLLM('{"final_answer": "续跑完成"}')
    loop = AgentLoop(config=_config(ws_tmp), llm=llm)
    goal = loop.create_goal("多轮目标", ["第二轮子目标"])
    try:
        await loop._maybe_continue_goal("多轮目标")
        stored = loop.goal_store.get(goal.id)
        assert stored.status is GoalStatus.COMPLETED
        assert stored.progress == 1.0
        assert any(e["type"] == "goal_idle_continue" for e in loop.events)
    finally:
        await loop.close()


@pytest.mark.asyncio
async def test_goal_disabled_keeps_planner_path(ws_tmp):
    """关闭目标驱动时行为不变：不创建台账，仍走外部 Planner。"""
    from agent.core.task import Task

    class StubPlanner:
        def __init__(self):
            self.calls = 0

        async def plan(self, prompt, context=""):
            self.calls += 1
            return [Task(id="t0", instruction=prompt, max_retries=0)]

    planner = StubPlanner()
    loop = AgentLoop(config=_config(ws_tmp, goal_enabled=False),
                     llm=ScriptedLLM('{"final_answer": "ok"}'),
                     planner=planner)
    try:
        result = await loop.run("普通任务")
        assert result.ok
        assert planner.calls == 1
        assert loop.goal_store is None
    finally:
        await loop.close()


@pytest.mark.asyncio
async def test_failed_goal_can_be_reactivated(ws_tmp):
    """失败目标可通过 --goal-id 对应入口重新激活，已完成目标不可复活。"""
    loop = AgentLoop(config=_config(ws_tmp), llm=ScriptedLLM("{}"))
    goal = loop.create_goal("可重试目标")
    try:
        loop._drive_goal_status(goal, GoalStatus.FAILED)
        loop.goal_store.upsert(goal)
        revived = loop.activate_goal(goal.id)
        assert revived is not None
        assert revived.status is GoalStatus.IN_PROGRESS

        loop._drive_goal_status(revived, GoalStatus.COMPLETED)
        loop.goal_store.upsert(revived)
        assert loop.activate_goal(goal.id) is None
    finally:
        await loop.close()
