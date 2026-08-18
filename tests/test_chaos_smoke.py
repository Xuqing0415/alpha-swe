# -*- coding: utf-8 -*-
"""收敛期 P1：CI 混沌阶段（阶段一 1.1 的随机化扩展）。

在真实基准集（tests/test_benchmark_suite.py）之上注入随机故障：
- 写入瞬态故障：FlakyWriteTool 让首次写入抛异常，验证 Agent 能通过
  「观察失败 -> 重试动作」恢复并最终通过完成标准；
- LLM 服务故障：FailingLLM 让整次任务走显式失败路径（带失败原因），
  验证不崩溃、不挂起；
- 聚合「优雅率」：完成（含恢复）或显式失败均算优雅，其余（崩溃/挂起）
  不算；优雅率必须高于阈值（默认 90%）。

规模通过环境变量控制（CHAOS_ITERATIONS / CHAOS_INJECT_RATE），
CI workflow 中调大，本地默认小规模快速跑。

运行：python -X utf8 -m pytest tests/test_chaos_smoke.py -q
"""
import asyncio
import json
import os
import random
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent.config import (AgentConfig, AppConfig, ContextConfig, MCPOptions,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.tools.fileio import FileIOTool
from agent.tools.manager import ToolManager

from test_benchmark_suite import CASES, write_files

pytestmark = pytest.mark.chaos

CHAOS_SEED = int(os.environ.get("CHAOS_SEED", "20260816"))
CHAOS_ITERATIONS = int(os.environ.get("CHAOS_ITERATIONS", "8"))
CHAOS_INJECT_RATE = float(os.environ.get("CHAOS_INJECT_RATE", "0.5"))
CHAOS_LLM_FAIL_RATE = float(os.environ.get("CHAOS_LLM_FAIL_RATE", "0.15"))
TARGET_GRACEFUL = float(os.environ.get("CHAOS_TARGET_GRACEFUL", "0.9"))

# 只取纯 Python 用例，避免混沌阶段依赖 node 运行时
PY_CASES = [c for c in CASES if c.tech.startswith("python")]
assert PY_CASES, "至少需要一个 python 基准用例"


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=1,
                     criticality="critical")]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class FailingLLM(MockLLM):
    """LLM 服务故障替身：始终抛超时异常。"""

    async def complete(self, messages):
        raise asyncio.TimeoutError("混沌注入：LLM 服务超时")


class FlakyWriteTool(FileIOTool):
    """首次写入抛异常的瞬态故障工具（之后恢复正常）。"""

    def __init__(self, fail_once: bool):
        super().__init__()
        self._fail_once = fail_once

    async def _write(self, target, content, start, task_id=""):
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("混沌注入：首次写入瞬态故障")
        return await super()._write(target, content, start,
                                    task_id=task_id)


def _think(text):
    return json.dumps({"think": text}, ensure_ascii=False)


def _write_action(rel: str, body: str) -> str:
    return json.dumps({"tool": "file_ops", "params": {
        "action": "write", "path": rel, "content": body}},
        ensure_ascii=False)


def _final(text):
    return json.dumps({"final_answer": text}, ensure_ascii=False)


def _summary(text):
    return json.dumps({"problem": text, "solution": "完成",
                       "steps": ["分析", "写入", "验证"],
                       "key_files": []}, ensure_ascii=False)


def _chaos_config(ws: Path) -> AppConfig:
    return AppConfig(
        agent=AgentConfig(
            max_rounds=10, max_retries=1, max_concurrency=1,
            trace_enabled=False, archive_enabled=False,
            metrics_enabled=True, snapshot_enabled=False,
            regression_check_enabled=False, auto_testgen=False,
            mutation_check_enabled=False, counterfactual_enabled=False,
        ),
        sandbox=SandboxConfig(workspace=str(ws),
                              audit_dir=str(ws / "logs" / "audit")),
        memory=MemoryConfig(db_path=str(ws / "mem.db"),
                            backend="none", auto_experience=False),
        mcp=MCPOptions(enabled=False),
        context=ContextConfig(archive_dir=str(ws / "logs" / "archives"),
                              max_tokens=400),
    )


async def _run_chaos_iteration(
    ws_tmp: Path, case, inject_write: bool, llm_fail: bool
) -> Dict[str, Any]:
    """单次混沌迭代：返回 {ok, graceful, detail}。"""
    write_files(ws_tmp, case.files)
    if llm_fail:
        llm: Any = FailingLLM()
        responses: List[str] = []
    else:
        # 脚本保留一次「重试写入」：首次写入被混沌故障打断后能自动恢复
        responses = [_think("定位问题后写入规范解法")]
        for rel, body in case.golden.items():
            responses.append(_write_action(rel, body))
            responses.append(_write_action(rel, body))
        responses.append(_final("已完成"))
        responses.append(_summary(case.task))
        llm = ScriptedLLM(*responses)
    tools = None
    if inject_write:
        tm = ToolManager(default_timeout=30.0)
        tm.register(FlakyWriteTool(fail_once=True))
        tools = tm
    loop = AgentLoop(config=_chaos_config(ws_tmp), llm=llm,
                     planner=StubPlanner(), tools=tools)
    try:
        result = await loop.run(case.task)
    except Exception as e:  # 混沌迭代自身异常：视为不优雅
        return {"ok": False, "graceful": False,
                "detail": "迭代异常 %s: %s" % (type(e).__name__, e)}
    finally:
        await loop.close()

    if result.ok:
        try:
            passed = case.verify(ws_tmp)
        except Exception:
            passed = False
        graceful = passed  # 恢复后必须真正通过完成标准
        return {"ok": True, "graceful": graceful,
                "detail": "完成 verify=%s" % passed}
    # 显式失败：final_answer 应包含失败原因（不崩溃/不挂起）
    graceful = bool(result.final_answer and len(result.final_answer) > 0)
    return {"ok": False, "graceful": graceful,
            "detail": "显式失败 phase=%s answer=%r" % (
                result.phase.name, result.final_answer[:80])}


@pytest.mark.asyncio
async def test_chaos_random_stream(ws_tmp):
    """随机故障流：在基准集子集上注入写入故障，验证自动恢复与显式降级。"""
    rng = random.Random(CHAOS_SEED)
    outcomes = []
    for _ in range(CHAOS_ITERATIONS):
        case = rng.choice(PY_CASES)
        inject_write = rng.random() < CHAOS_INJECT_RATE
        llm_fail = rng.random() < CHAOS_LLM_FAIL_RATE
        out = await _run_chaos_iteration(ws_tmp, case, inject_write, llm_fail)
        outcomes.append((case.name, inject_write, llm_fail, out))
    graceful = sum(1 for _, _, _, o in outcomes if o["graceful"])
    rate = graceful / len(outcomes)
    print("\n[混沌] 随机故障流优雅率: %d/%d = %.1f%%"
          % (graceful, len(outcomes), rate * 100))
    for name, inj, llm_fail, o in outcomes:
        print("  [%s] %s inject_write=%s llm_fail=%s ok=%s: %s"
              % ("OK" if o["graceful"] else "BAD",
                 name, inj, llm_fail, o["ok"], o["detail"]))
    assert rate >= TARGET_GRACEFUL, (
        "混沌优雅率 %.1f%% 低于目标 %.0f%%" % (rate * 100,
                                             TARGET_GRACEFUL * 100))


@pytest.mark.asyncio
async def test_chaos_llm_failure_degrades_cleanly(ws_tmp):
    """LLM 服务故障：任务显式失败且带失败原因，不崩溃不挂起。"""
    case = PY_CASES[0]
    out = await _run_chaos_iteration(ws_tmp, case, inject_write=False,
                                     llm_fail=True)
    assert out["ok"] is False
    assert out["graceful"], out["detail"]
    assert "迭代异常" not in out["detail"]
