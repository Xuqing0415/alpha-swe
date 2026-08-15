# -*- coding: utf-8 -*-
"""进阶 3.1：自动测试生成测试。

覆盖：AST 目标提取（跳过私有/main）、生成内容、写入与需求判断、
loop 集成（写入 .py 自动生成 test_*.py、已有测试时跳过）。
"""
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.testgen import (extract_targets, generate_and_write,
                            generate_tests, needs_tests)


SOURCE = (
    "def helper(x=1):\n"
    "    return x\n"
    "\n"
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "def _private():\n"
    "    return 0\n"
    "\n"
    "def main():\n"
    "    print('noop')\n"
)


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp, **mem_kw):
    return AppConfig(
        agent=AgentConfig(max_rounds=5, max_retries=2),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"), **mem_kw),
        mcp=MCPOptions(enabled=False),
    )


# ---- AST 目标提取 ----

def test_extract_targets_public_only():
    targets = extract_targets(SOURCE)
    names = {t["name"] for t in targets}
    assert names == {"helper", "add"}
    by_name = {t["name"]: t for t in targets}
    assert by_name["helper"]["all_defaults"] is True
    assert by_name["helper"]["pure"] is True
    assert by_name["add"]["n_required"] == 2
    assert by_name["add"]["all_defaults"] is False
    assert "main" not in names and "_private" not in names


def test_extract_targets_syntax_error_returns_empty():
    assert extract_targets("def broken(:\n") == []


# ---- 生成内容 ----

def test_generate_tests_content():
    content = generate_tests(SOURCE, "calc.py")
    assert "MODULE = importlib.import_module('calc')" in content
    assert "def test_module_importable():" in content
    assert "def test_helper_callable():" in content
    assert "def test_helper_default_call():" in content
    assert "def test_add_callable():" in content
    assert "def test_add_default_call():" not in content
    assert "test_private" not in content and "test_main" not in content


def test_generate_tests_empty_for_no_targets():
    assert generate_tests("_x = 1\n", "m.py") == ""
    assert generate_tests("", "m.py") == ""


def test_generate_tests_non_identifier_module():
    assert generate_tests("def f():\n    return 1\n", "2fa.py") == ""


# ---- 写入与需求判断 ----

def test_generate_and_write_creates_test_file(ws_tmp):
    ws = str(ws_tmp / "ws")
    (Path(ws) / "src").mkdir(parents=True, exist_ok=True)
    result = generate_and_write(ws, "src/calc.py", SOURCE)
    assert result is not None
    test_path, targets = result
    assert targets == 2
    test_file = Path(ws) / "src" / "test_calc.py"
    assert test_file.exists()
    content = test_file.read_text(encoding="utf-8")
    assert "def test_add_callable():" in content


def test_generate_and_write_none_for_no_symbols(ws_tmp):
    ws = str(ws_tmp / "ws")
    Path(ws).mkdir(parents=True, exist_ok=True)
    assert generate_and_write(ws, "empty.py", "x = 1\n") is None
    assert not (Path(ws) / "test_empty.py").exists()


def test_needs_tests(ws_tmp):
    ws = str(ws_tmp / "ws")
    (Path(ws) / "src").mkdir(parents=True, exist_ok=True)
    (Path(ws) / "src" / "app.py").write_text("x=1\n", encoding="utf-8")
    assert needs_tests(ws, "src/app.py") is True
    (Path(ws) / "src" / "test_app.py").write_text("", encoding="utf-8")
    assert needs_tests(ws, "src/app.py") is False
    assert needs_tests(ws, "src/test_app.py") is False
    # tests/ 目录布局兜底
    (Path(ws) / "tests").mkdir(exist_ok=True)
    (Path(ws) / "tests" / "test_app.py").write_text("", encoding="utf-8")
    assert needs_tests(ws, "src/app.py") is False


# ---- loop 集成 ----

@pytest.mark.asyncio
async def test_loop_auto_testgen_on_py_write(ws_tmp):
    cfg = make_config(ws_tmp, backend="sqlite")
    llm = ScriptedLLM(
        '{"tool": "file_ops", "params": {"action": "write", "path": "calc.py", '
        '"content": "def add(a, b):\\n    return a + b\\n"}}',
        '{"final_answer": "done"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("写一个 calc.py")
    assert result.ok
    test_file = Path(cfg.sandbox.workspace) / "test_calc.py"
    assert test_file.exists()
    assert "def test_add_callable():" in test_file.read_text(encoding="utf-8")
    names = {d["name"] for d in loop._decision.records()}
    assert "testgen.generated" in names


@pytest.mark.asyncio
async def test_loop_auto_testgen_skips_existing(ws_tmp):
    cfg = make_config(ws_tmp, backend="sqlite")
    ws = Path(cfg.sandbox.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "test_calc.py").write_text("", encoding="utf-8")
    llm = ScriptedLLM(
        '{"tool": "file_ops", "params": {"action": "write", "path": "calc.py", '
        '"content": "def add(a, b):\\n    return a + b\\n"}}',
        '{"final_answer": "done"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("写一个 calc.py")
    assert result.ok
    names = {d["name"] for d in loop._decision.records()}
    assert "testgen.skip" in names
    assert "testgen.generated" not in names
