# -*- coding: utf-8 -*-
"""会话间工作流连续性（主线一 1.2）：WorkspaceContext 单测 + 续接提示集成测试。"""
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop, LoopResult
from agent.core.state import AgentPhase
from agent.llm import MockLLM
from agent.workspace_context import WorkspaceContext


def make_config(ws_tmp: Path):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


class StubPlanner:
    async def plan(self, prompt, context=""):
        from agent.core.task import Task
        return [Task(id="t0", instruction=prompt, criticality="critical")]


class RecordingLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


@pytest.fixture
def ws_dir(ws_tmp):
    d = ws_tmp / "ws"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_roundtrip_and_fields(ws_dir):
    ctx = WorkspaceContext(str(ws_dir))
    assert ctx.data == {}
    ctx.begin("修复登录 bug")
    ctx.data["current_task_id"] = "task_001"
    ctx.save()
    ctx2 = WorkspaceContext(str(ws_dir))
    assert ctx2.data["prompt"] == "修复登录 bug"
    assert (ws_dir / ".swe-agent" / "context.json").is_file()


def test_is_active_after_interrupt(ws_dir):
    ctx = WorkspaceContext(str(ws_dir))
    ctx.begin("重构用户模块")
    ctx.finalize("重构用户模块", None)  # 会话被中断
    assert ctx.data["status"] == "active"
    assert ctx.data["task_phase"] == "interrupted"
    assert ctx.is_active() is True
    text = ctx.summarize()
    assert "上次你在做" in text and "建议继续" in text
    assert "上次会话的未完成任务" in ctx.prompt_text()


def test_completed_marks_inactive(ws_dir):
    ctx = WorkspaceContext(str(ws_dir))
    ctx.begin("加一个接口")
    result = LoopResult(final_answer="接口完成", phase=AgentPhase.COMPLETED)
    ctx.finalize("加一个接口", result)
    assert ctx.data["status"] == "completed"
    assert ctx.is_active() is False
    assert "任务已完成" in ctx.data["next_session_hint"]


def test_failed_generates_hint(ws_dir):
    from agent.core.task import Task, TaskStatus
    ctx = WorkspaceContext(str(ws_dir))
    ctx.begin("修复空指针")
    failed_task = Task(id="t0", instruction="修复空指针", status=TaskStatus.FAILED,
                       error="IndexError: list index out of range")
    result = LoopResult(phase=AgentPhase.FAILED, tasks=[failed_task])
    ctx.finalize("修复空指针", result)
    assert ctx.data["status"] == "active"
    assert "IndexError" in ctx.data["next_session_hint"]
    # 1.2B：待办已结构化（含动作类型/阻塞标记）
    assert ctx.data["pending_actions"][0]["instruction"] == "修复空指针"
    assert ctx.data["pending_actions"][0]["blocking"] is True


def test_pending_actions_excludes_completed(ws_dir):
    from agent.core.task import Task, TaskStatus
    ctx = WorkspaceContext(str(ws_dir))
    ctx.begin("双任务")
    done = Task(id="a", instruction="已完成任务", status=TaskStatus.COMPLETED)
    todo = Task(id="b", instruction="未完成任务", status=TaskStatus.READY)
    result = LoopResult(phase=AgentPhase.FAILED, tasks=[done, todo])
    ctx.finalize("双任务", result)
    instructions = [p["instruction"] for p in ctx.data["pending_actions"]]
    assert "未完成任务" in instructions
    assert "已完成任务" not in instructions


@pytest.mark.asyncio
async def test_loop_resume_hint_event_and_injection(ws_tmp):
    cfg = make_config(ws_tmp)
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    # 模拟上次会话被中断留下的上下文
    prev = WorkspaceContext(str(ws))
    prev.begin("上次没做完的任务")
    prev.finalize("上次没做完的任务", None)

    llm = RecordingLLM('{"final_answer": "done"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    r = await loop.run("继续上次的任务")
    assert r.ok
    await loop.close()
    # 事件 + 决策日志 + Prompt 注入
    assert any(e["type"] == "workspace_resume_hint" for e in loop.events)
    names = [r["name"] for r in loop._decision.records()]
    assert "workspace.resume" in names
    system = llm.calls[0][0]["content"]
    assert "上次会话的未完成任务" in system
    # 会话结束后：任务完成，上下文标记完成，不再提示
    ctx = WorkspaceContext(str(ws))
    assert ctx.data["status"] == "completed"
    assert ctx.is_active() is False


@pytest.mark.asyncio
async def test_loop_no_hint_without_previous_context(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = RecordingLLM('{"final_answer": "done"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    r = await loop.run("全新任务")
    assert r.ok
    await loop.close()
    assert not any(e["type"] == "workspace_resume_hint" for e in loop.events)
    ctx = WorkspaceContext(str(ws_tmp / "ws"))
    assert ctx.data.get("status") == "completed"
