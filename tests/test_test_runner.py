"""测试与验证闭环测试（方案 3.3）：统一运行接口 + 失败分析 + 修复重测。"""
from pathlib import Path

import pytest

from agent.code.test_runner import parse_test_output, run_tests
from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.tools.base import ExecutionContext
from agent.tools.test_tool import TestRunnerTool

PYTEST_FAILURE_SAMPLE = """============================= test session starts ==============================
collected 2 items

test_sample.py F.                                                       [100%]

=================================== FAILURES ===================================
_________________________________ test_bad ____________________________________

    def test_bad():
>       assert 1 == 2
E       assert 1 == 2

test_sample.py:7: AssertionError
=========================== short test summary info ===========================
FAILED test_sample.py::test_bad - AssertionError: assert 1 == 2
========================= 1 failed, 1 passed in 0.05s ==========================
"""


def test_parse_pytest_failures():
    failures = parse_test_output("pytest", PYTEST_FAILURE_SAMPLE)
    assert len(failures) == 1
    f = failures[0]
    assert f.name == "test_sample.py::test_bad"
    assert "assert 1 == 2" in (f.reason + f.assertion)


def test_parse_jest_like_failures():
    out = "✕ renders component (3ms)\n  ● renders component\n    expect(received).toBe(expected)"
    failures = parse_test_output("jest", out)
    assert len(failures) >= 1
    assert "renders component" in failures[0].name


@pytest.mark.asyncio
async def test_run_tests_end_to_end(ws_tmp):
    (ws_tmp / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n\n"
        "def test_bad():\n    assert 1 == 2\n",
        encoding="utf-8",
    )
    result = await run_tests("pytest", "test_sample.py", str(ws_tmp))
    assert result.success is False
    assert result.framework == "pytest"
    assert any("test_bad" in f.name for f in result.failures)


@pytest.mark.asyncio
async def test_run_tests_passes_after_fix(ws_tmp):
    (ws_tmp / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n\n"
        "def test_bad():\n    assert 1 == 2\n",
        encoding="utf-8",
    )
    failed = await run_tests("pytest", "test_sample.py", str(ws_tmp))
    assert failed.success is False
    (ws_tmp / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n\n"
        "def test_bad():\n    assert 1 == 1\n",
        encoding="utf-8",
    )
    ok = await run_tests("pytest", "test_sample.py", str(ws_tmp))
    assert ok.success is True
    assert ok.coverage is None


@pytest.mark.asyncio
async def test_test_runner_tool_result_summary(ws_tmp):
    (ws_tmp / "test_sample.py").write_text(
        "def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    tool = TestRunnerTool(decision_logger=None)
    ctx = ExecutionContext(workspace=str(ws_tmp))
    r = await tool.execute({"framework": "pytest",
                            "target": "test_sample.py"}, ctx)
    assert r.success is False
    assert "测试失败" in r.output
    assert "test_bad" in r.output


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


@pytest.mark.asyncio
async def test_loop_fix_and_retest_cycle(ws_tmp):
    """方案 3.3：测试失败 -> 修改代码 -> 重测通过 -> 完成。"""
    (ws_tmp / "ws").mkdir(parents=True, exist_ok=True)
    (ws_tmp / "ws" / "test_sample.py").write_text(
        "def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    cfg = AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )
    fix = "def test_bad():\\n    assert 1 == 1\\n"
    llm = ScriptedLLM(
        '{"tool": "run_tests", "params": {"framework": "pytest"}}',
        '{"tool": "file_ops", "params": {"action": "write", '
        f'"path": "test_sample.py", "content": "{fix}"}}',
        '{"tool": "run_tests", "params": {"framework": "pytest"}}',
        '{"final_answer": "测试已修复并通过"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("修复失败的测试")
    assert result.ok
    assert "测试已修复并通过" in result.final_answer
    # 决策日志记录了两次测试运行（一次失败一次通过）
    runs = [d for d in loop._decision.records() if d.get("name") == "test.run"]
    assert len(runs) == 2
