# -*- coding: utf-8 -*-
"""阶段三 3.3：回归检测测试。

覆盖：受影响测试文件定位（同目录/tests 布局/缺失）、三态分类（clean/
regression/skip）、loop 集成（写入 .py 后自动检测、回归失败回写该步骤
结果、clean/skip 决策点、非 .py 写入不触发）。
"""
from pathlib import Path

import pytest

from agent.code.test_runner import TestCaseFailure, TestResult
from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.regression import affected_test_path, classify_test_result


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp, **agent_kw):
    return AppConfig(
        agent=AgentConfig(max_rounds=5, max_retries=2, **agent_kw),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"), backend="sqlite"),
        mcp=MCPOptions(enabled=False),
    )


WRITE = (
    '{"tool": "file_ops", "params": {"action": "write", "path": "calc.py", '
    '"content": "def add(a, b):\\n    return a + b\\n"}}'
)


# ---- 受影响测试定位 ----

def test_affected_test_path_same_dir(ws_tmp):
    ws = str(ws_tmp / "ws")
    (Path(ws) / "src").mkdir(parents=True, exist_ok=True)
    (Path(ws) / "src" / "app.py").write_text("x=1\n", encoding="utf-8")
    (Path(ws) / "src" / "test_app.py").write_text("", encoding="utf-8")
    assert affected_test_path(ws, "src/app.py") == "src/test_app.py"


def test_affected_test_path_tests_layout(ws_tmp):
    ws = str(ws_tmp / "ws")
    (Path(ws) / "src").mkdir(parents=True, exist_ok=True)
    (Path(ws) / "tests").mkdir(exist_ok=True)
    (Path(ws) / "src" / "app.py").write_text("x=1\n", encoding="utf-8")
    (Path(ws) / "tests" / "test_app.py").write_text("", encoding="utf-8")
    assert affected_test_path(ws, "src/app.py") == "tests/test_app.py"


def test_affected_test_path_missing_or_test_module(ws_tmp):
    ws = str(ws_tmp / "ws")
    Path(ws).mkdir(parents=True, exist_ok=True)
    (Path(ws) / "app.py").write_text("x=1\n", encoding="utf-8")
    assert affected_test_path(ws, "app.py") is None
    assert affected_test_path(ws, "test_app.py") is None


# ---- 三态分类 ----

def test_classify_test_result():
    assert classify_test_result(TestResult(success=True)) == "clean"
    fail = TestResult(success=False, output="FAILED tests/test_x.py::t",
                      failures=[TestCaseFailure(name="test_x::t")])
    assert classify_test_result(fail) == "regression"
    assert classify_test_result(
        TestResult(success=False, output="启动测试失败: PermissionError")
    ) == "skip"
    assert classify_test_result(
        TestResult(success=False, output="=== 1 failed in 2.1s ===")
    ) == "regression"
    assert classify_test_result(
        TestResult(success=False, output="no tests ran in 0.01s")) == "skip"


# ---- loop 集成 ----

@pytest.mark.asyncio
async def test_loop_regression_skip_when_tests_cannot_run(ws_tmp):
    cfg = make_config(ws_tmp, auto_testgen=False)
    ws = Path(cfg.sandbox.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "test_calc.py").write_text("", encoding="utf-8")
    # 沙箱拦截子进程 -> run_tests 返回启动失败 -> skip，任务不失败
    llm = ScriptedLLM(WRITE, '{"final_answer": "done"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("写一个 calc.py")
    assert result.ok
    names = {d["name"] for d in loop._decision.records()}
    assert "regression.skip" in names


@pytest.mark.asyncio
async def test_loop_regression_clean(ws_tmp, monkeypatch):
    cfg = make_config(ws_tmp, auto_testgen=False)
    ws = Path(cfg.sandbox.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "test_calc.py").write_text("", encoding="utf-8")

    async def fake_run_tests(framework, target, workspace, timeout=60.0, **kw):
        return TestResult(success=True, output="1 passed", framework="pytest")

    monkeypatch.setattr("agent.code.test_runner.run_tests", fake_run_tests)
    llm = ScriptedLLM(WRITE, '{"final_answer": "done"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("写一个 calc.py")
    assert result.ok
    names = {d["name"] for d in loop._decision.records()}
    assert "regression.clean" in names
    assert any(e["type"] == "regression_clean" for e in loop.events)


@pytest.mark.asyncio
async def test_loop_regression_detected_fails_step(ws_tmp, monkeypatch):
    cfg = make_config(ws_tmp, auto_testgen=False)
    ws = Path(cfg.sandbox.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "test_calc.py").write_text("", encoding="utf-8")

    async def fake_run_tests(framework, target, workspace, timeout=60.0, **kw):
        return TestResult(
            success=False,
            output="FAILED test_calc.py::test_add_callable - AssertionError",
            failures=[TestCaseFailure(
                name="test_calc.py::test_add_callable",
                reason="AssertionError")],
            framework="pytest")

    monkeypatch.setattr("agent.code.test_runner.run_tests", fake_run_tests)
    # 回归失败回写为该步骤结果 -> Agent 在观察中看到 [回归检测]
    llm = ScriptedLLM(WRITE, '{"final_answer": "完成"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("写一个 calc.py")
    assert result.ok
    names = {d["name"] for d in loop._decision.records()}
    assert "regression.detected" in names
    assert any(e["type"] == "regression_detected" for e in loop.events)
    task = loop.scheduler.dag.get("t0")
    assert any("[回归检测]" in h.get("content", "") for h in task.history)


@pytest.mark.asyncio
async def test_loop_regression_not_triggered_for_txt(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"tool": "file_ops", "params": {"action": "write", "path": "a.txt", '
        '"content": "hi"}}',
        '{"final_answer": "done"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("写 txt")
    assert result.ok
    names = {d["name"] for d in loop._decision.records()}
    assert not any(n.startswith("regression.") for n in names)
