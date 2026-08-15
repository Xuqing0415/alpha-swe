# -*- coding: utf-8 -*-
"""阶段三 3.2：变异测试测试。

覆盖：确定性变异生成（反转比较/算术/布尔/常量、限数、语法错误容错）、
检出率计算、run_mutation_analysis（基线失败跳过/检出统计/变异后文件恢复）、
loop 集成（生成测试后自动变异检测并记录 mutation.analyzed 决策点）。
"""
from pathlib import Path

import pytest

from agent.code.test_runner import TestResult
from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.mutation import apply_mutations, mutation_score
from agent.mutation import run_mutation_analysis


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


SRC = (
    "def check(x, y):\n"
    "    if x > 0 and y == 0:\n"
    "        return x + 1\n"
    "    return False\n"
)


# ---- 确定性变异 ----

def test_apply_mutations_flips_operators():
    mutations = apply_mutations(SRC, limit=10)
    names = [m["name"] for m in mutations]
    assert len(mutations) >= 4
    assert any("Gt" in n for n in names)              # > -> <=
    assert any("Eq" in n for n in names)              # == -> !=
    assert any("Add" in n for n in names)             # + -> -
    assert any(n.startswith("const:") for n in names)  # True/False 翻转
    for m in mutations:
        assert m["mutated"] != SRC


def test_apply_mutations_limit_and_syntax_error():
    assert len(apply_mutations(SRC, limit=2)) == 2
    assert apply_mutations("def broken(:\n") == []


def test_mutation_score():
    assert mutation_score({"skipped": True}) is None
    assert mutation_score({"skipped": False, "total": 0, "killed": 0}) is None
    assert mutation_score({"skipped": False, "total": 4, "killed": 3}) == 0.75


# ---- 分析运行 ----

@pytest.mark.asyncio
async def test_run_mutation_analysis_skips_when_baseline_fails(ws_tmp,
                                                               monkeypatch):
    ws = str(ws_tmp / "ws")
    Path(ws).mkdir(parents=True, exist_ok=True)
    module = Path(ws) / "calc.py"
    module.write_text(SRC, encoding="utf-8")
    (Path(ws) / "test_calc.py").write_text("", encoding="utf-8")

    async def fail(*args, **kwargs):
        return TestResult(success=False, output="启动测试失败: PermissionError")

    monkeypatch.setattr("agent.mutation.run_tests", fail)
    analysis = await run_mutation_analysis(ws, "calc.py", SRC, "test_calc.py")
    assert analysis["skipped"] is True
    assert module.read_text(encoding="utf-8") == SRC


@pytest.mark.asyncio
async def test_run_mutation_analysis_detects_and_restores(ws_tmp, monkeypatch):
    ws = str(ws_tmp / "ws")
    Path(ws).mkdir(parents=True, exist_ok=True)
    module = Path(ws) / "calc.py"
    module.write_text(SRC, encoding="utf-8")
    (Path(ws) / "test_calc.py").write_text("", encoding="utf-8")

    state = {"calls": 0}

    async def fake(framework, target, workspace, timeout=60.0, **kw):
        state["calls"] += 1
        if state["calls"] == 1:
            return TestResult(success=True, output="1 passed")
        return TestResult(success=False, output="FAILED test_calc.py - x")

    monkeypatch.setattr("agent.mutation.run_tests", fake)
    analysis = await run_mutation_analysis(ws, "calc.py", SRC, "test_calc.py")
    assert analysis["skipped"] is False
    assert analysis["total"] > 0
    assert analysis["killed"] == analysis["total"]
    assert analysis["score"] == 1.0
    assert analysis["survivors"] == []
    assert module.read_text(encoding="utf-8") == SRC


@pytest.mark.asyncio
async def test_run_mutation_analysis_survivors(ws_tmp, monkeypatch):
    ws = str(ws_tmp / "ws")
    Path(ws).mkdir(parents=True, exist_ok=True)
    module = Path(ws) / "calc.py"
    module.write_text(SRC, encoding="utf-8")
    (Path(ws) / "test_calc.py").write_text("", encoding="utf-8")

    async def fake(framework, target, workspace, timeout=60.0, **kw):
        return TestResult(success=True, output="1 passed")

    monkeypatch.setattr("agent.mutation.run_tests", fake)
    analysis = await run_mutation_analysis(ws, "calc.py", SRC, "test_calc.py")
    assert analysis["skipped"] is False
    assert analysis["killed"] == 0
    assert analysis["score"] == 0.0
    assert len(analysis["survivors"]) == analysis["total"] > 0
    assert module.read_text(encoding="utf-8") == SRC


# ---- loop 集成 ----

@pytest.mark.asyncio
async def test_loop_mutation_analysis_records_decision(ws_tmp, monkeypatch):
    cfg = make_config(ws_tmp)  # auto_testgen + mutation_check 默认开启
    ws = Path(cfg.sandbox.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    state = {"calls": 0}

    async def fake(framework, target, workspace, timeout=60.0, **kw):
        state["calls"] += 1
        if state["calls"] == 1:
            return TestResult(success=True, output="1 passed")
        return TestResult(success=False, output="FAILED test_calc.py - x")

    monkeypatch.setattr("agent.mutation.run_tests", fake)
    llm = ScriptedLLM(
        '{"tool": "file_ops", "params": {"action": "write", "path": "calc.py", '
        '"content": "def add(a, b):\\n    return a + b\\n"}}',
        '{"final_answer": "done"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("写一个 calc.py")
    assert result.ok
    names = {d["name"] for d in loop._decision.records()}
    assert "testgen.generated" in names
    assert "mutation.analyzed" in names
    rec = next(d for d in loop._decision.records()
               if d["name"] == "mutation.analyzed")
    assert "100%" in rec["decision"] or "1/1" in rec["decision"]
    assert any(e["type"] == "mutation_analyzed" for e in loop.events)
