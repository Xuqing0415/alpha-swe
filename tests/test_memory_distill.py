# -*- coding: utf-8 -*-
"""1.3C 会话状态提炼：一次性状态过滤、LLM 提炼与项目记忆落库。"""
import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.session_state import SessionState
from agent.core.task import Task
from agent.llm import MockLLM
from agent.memory.distill import collect_candidates, distill_session


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        return self._responses.pop(0) if self._responses else '{"items": []}'


def _config(ws, distill=True):
    return AppConfig(
        agent=AgentConfig(
            max_rounds=2, max_retries=0, self_improve_enabled=False,
            state_tracker_enabled=False, workspace_context_enabled=False,
            snapshot_enabled=False, session_distill_enabled=distill,
        ),
        sandbox=SandboxConfig(workspace=str(ws), docker_enabled=False),
        memory=MemoryConfig(backend="sqlite", db_path=str(ws / "mem.db"),
                            auto_experience=False, layered=True),
        mcp=MCPOptions(enabled=False),
    )


def test_collect_candidates_filters_ephemeral_state():
    state = SessionState(evidence={
        "project.convention": "项目统一使用 AppError 包装领域错误",
        "debug.trace": "刚才测试失败在 auth.test.ts 第 42 行",
    })
    result = type("R", (), {
        "final_answer": "已完成登录接口，项目使用 AppError 处理错误",
        "tasks": [],
    })()
    items = collect_candidates(state, result, "修复登录接口")
    text = "\n".join(items)
    assert "AppError" in text
    assert "第 42 行" not in text
    assert "刚才" not in text


@pytest.mark.asyncio
async def test_distill_session_uses_llm_and_redacts():
    state = SessionState(evidence={"project.convention": "统一使用 AppError"})
    result = type("R", (), {
        "final_answer": "项目使用 AppError 处理领域错误",
        "tasks": [],
    })()
    llm = ScriptedLLM('{"items": ["项目统一使用 AppError 包装领域错误", '
                      '"密钥 sk-' + "A" * 32 + ' 不应保留"]}')
    items = await distill_session(llm, state, result, "修复登录")
    assert any("AppError" in item for item in items)
    assert all("sk-" + "A" * 16 not in item for item in items)


@pytest.mark.asyncio
async def test_loop_session_distill_writes_project_memory(ws_tmp):
    llm = ScriptedLLM(
        '{"final_answer": "项目统一使用 AppError 处理领域错误"}',
        '{"items": ["项目统一使用 AppError 包装领域错误"]}',
    )
    class StubPlanner:
        async def plan(self, prompt, context=""):
            return [Task(id="t0", instruction=prompt, max_retries=0)]

    loop = AgentLoop(config=_config(ws_tmp), llm=llm, planner=StubPlanner())
    try:
        result = await loop.run("修复登录接口")
        assert result.ok
        assert any(d["name"] == "session.distilled"
                   for d in loop._decision.records())
        hits = loop.memory.search("AppError", top_k=5)
        assert any("AppError" in str(hit.get("text", "")) for hit in hits)
    finally:
        await loop.close()
