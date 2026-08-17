# -*- coding: utf-8 -*-
"""项目状态感知（主线一 1.1）：ProjectStateTracker 单测 + 跨会话注入集成测试。"""
import json
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.llm import MockLLM
from agent.project_state import ProjectStateTracker


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


def test_first_begin_creates_baseline_no_diff(ws_dir):
    tracker = ProjectStateTracker(str(ws_dir))
    diff = tracker.begin_session()
    assert diff == {}
    state = tracker.state
    assert state["schema"] == 1
    assert (ws_dir / ".swe-agent" / "state.json").is_file()
    assert "structure" in state and "deps" in state


def test_dep_version_change_detected_across_sessions(ws_dir):
    pkg = ws_dir / "package.json"
    pkg.write_text(json.dumps({"dependencies": {"express": "4.18.2"}}),
                   encoding="utf-8")
    tracker = ProjectStateTracker(str(ws_dir))
    tracker.begin_session()  # 首次基线

    pkg.write_text(json.dumps({"dependencies": {"express": "4.19.0"}}),
                   encoding="utf-8")
    tracker2 = ProjectStateTracker(str(ws_dir))
    diff = tracker2.begin_session()
    assert len(diff["deps"]) == 1
    d = diff["deps"][0]
    assert d["dep"] == "express" and d["old"] == "4.18.2" and d["new"] == "4.19.0"
    text = tracker2.diff_text(diff)
    assert "express 4.18.2 -> 4.19.0" in text
    assert "breaking changes" in text
    assert any(h["dep"] == "express" for h in tracker2.state["dependency_history"])


def test_file_change_detected_across_sessions(ws_dir):
    (ws_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
    tracker = ProjectStateTracker(str(ws_dir))
    tracker.begin_session()  # 基线含 a.py

    (ws_dir / "a.py").write_text("x = 2\n", encoding="utf-8")
    (ws_dir / "b.py").write_text("y = 1\n", encoding="utf-8")
    tracker2 = ProjectStateTracker(str(ws_dir))
    diff = tracker2.begin_session()
    assert "a.py" in diff["files"]["modified"]
    assert "b.py" in diff["files"]["added"]
    text = tracker2.diff_text(diff)
    assert "a.py" in text and "b.py" in text


def test_end_session_records_recent_changes(ws_dir):
    tracker = ProjectStateTracker(str(ws_dir))
    tracker.begin_session()
    (ws_dir / "new.py").write_text("pass\n", encoding="utf-8")
    changes = tracker.end_session()
    kinds = {c["kind"] for c in changes["files"]}
    assert "added" in kinds
    assert any(c["path"] == "new.py" for c in tracker.state["recent_changes"])


def test_tech_stack_and_health_and_debt(ws_dir):
    (ws_dir / "requirements.txt").write_text("fastapi\n",
                                             encoding="utf-8")
    tracker = ProjectStateTracker(str(ws_dir))
    tracker.begin_session()
    assert "FastAPI" in tracker.state["tech_stack"]
    tracker.record_test_result(passed=10, failed=0, coverage=80.0)
    health = tracker.state["test_health"]
    assert health["passed"] == 10 and health["coverage"] == 80.0
    tracker.add_tech_debt("重构 auth 模块")
    assert tracker.list_tech_debt()[0]["status"] == "open"
    assert tracker.resolve_tech_debt("重构 auth 模块") is True
    assert tracker.list_tech_debt()[0]["status"] == "resolved"


def test_skip_dirs_not_in_snapshot(ws_dir):
    (ws_dir / "keep.py").write_text("x=1\n", encoding="utf-8")
    (ws_dir / "node_modules").mkdir(exist_ok=True)
    (ws_dir / "node_modules" / "junk.js").write_text("y=2\n", encoding="utf-8")
    (ws_dir / ".venv").mkdir(exist_ok=True)
    (ws_dir / ".venv" / "site.py").write_text("z=3\n", encoding="utf-8")
    tracker = ProjectStateTracker(str(ws_dir))
    snapshot = tracker.scan()
    paths = set(snapshot["structure"])
    assert "keep.py" in paths
    assert not any("node_modules" in p or ".venv" in p for p in paths)


@pytest.mark.asyncio
async def test_loop_injects_project_state_diff_on_second_session(ws_tmp):
    cfg = make_config(ws_tmp)
    # 第一会话：写一个文件
    llm1 = RecordingLLM(
        '{"tool": "file_ops", "params": {"action": "write", '
        '"path": "hello.txt", "content": "hi"}}',
        '{"final_answer": "done"}',
    )
    loop1 = AgentLoop(config=cfg, llm=llm1, planner=StubPlanner())
    r1 = await loop1.run("写 hello.txt")
    assert r1.ok
    await loop1.close()
    assert (ws_tmp / "ws" / "hello.txt").is_file()

    # 会话之间：外部新增文件（模拟其他进程/用户改动）
    (ws_tmp / "ws" / "external.py").write_text("x = 1\n", encoding="utf-8")

    # 第二会话：应注入"上次会话以来的项目变化"
    llm2 = RecordingLLM('{"final_answer": "done2"}')
    loop2 = AgentLoop(config=cfg, llm=llm2, planner=StubPlanner())
    r2 = await loop2.run("第二个任务")
    assert r2.ok
    await loop2.close()
    system = llm2.calls[0][0]["content"]
    assert "上次会话以来的项目变化" in system
    assert "external.py" in system
    # 决策日志记录差异
    names = [r["name"] for r in loop2._decision.records()]
    assert "project_state.diff" in names
    # 事件可见
    assert any(e["type"] == "project_state_diff" for e in loop2.events)


@pytest.mark.asyncio
async def test_loop_disabled_flags_no_tracker(ws_tmp):
    cfg = make_config(ws_tmp)
    cfg.agent.state_tracker_enabled = False
    cfg.agent.workspace_context_enabled = False
    llm = RecordingLLM('{"final_answer": "done"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    assert loop.project_state_tracker is None
    assert loop.workspace_context is None
    r = await loop.run("任务")
    assert r.ok
    await loop.close()
    assert not (ws_tmp / "ws" / ".swe-agent" / "state.json").exists()
    assert not (ws_tmp / "ws" / ".swe-agent" / "context.json").exists()