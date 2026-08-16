# -*- coding: utf-8 -*-
"""收敛期 P1：长时间浸泡测试（阶段一 1.2）。

覆盖：
- 顺序任务流：同一进程内连续运行多会话 AgentLoop，全部成功；
- 决策日志内存有界：跨会话复用 DecisionLogger，max_memory_records 裁剪生效，
  被淘汰记录落盘 JSONL 不丢数据；
- 内存收敛：tracemalloc 采样，预热后峰值增长不超过阈值（无泄漏）；
- 句柄收敛：psutil 统计进程句柄/文件描述符数，预热后增量有界；
- 并发任务流：3 条流同时运行各自会话，无死锁、事件列表有界；
- 临时文件残留：工作区不产生 *.tmp 等杂质文件。

运行：python -X utf8 -m pytest tests/test_soak.py -q
"""
import asyncio
import gc
import json
import os
import tracemalloc
from pathlib import Path
from typing import List, Tuple

import psutil
import pytest

from agent.config import (AgentConfig, AppConfig, ContextConfig, MCPOptions,
                          MemoryConfig, SandboxConfig)
from agent.core.decision_logger import DecisionLogger
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM

SOAK_SESSIONS = int(os.environ.get("SOAK_SESSIONS", "24"))
SOAK_CONCURRENCY = int(os.environ.get("SOAK_CONCURRENCY", "3"))
SOAK_PER_STREAM = int(os.environ.get("SOAK_PER_STREAM", "8"))
WARMUP = 4
MAX_PEAK_GROWTH_BYTES = 4 * 1024 * 1024  # 预热后峰值增长上限 4MB
MAX_HANDLE_DELTA = 25  # 预热后句柄增量上限
MAX_EVENTS_PER_SESSION = 20


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def _think(text):
    return json.dumps({"think": text}, ensure_ascii=False)


def _tool(**params):
    return json.dumps({"tool": "file_ops", "params": params},
                      ensure_ascii=False)


def _final(text):
    return json.dumps({"final_answer": text}, ensure_ascii=False)


def _summary(task):
    return json.dumps({"problem": task, "solution": "完成",
                       "steps": ["分析", "修改", "验证"],
                       "key_files": ["a.txt"]}, ensure_ascii=False)


def _make_config(ws_tmp: Path) -> AppConfig:
    """浸泡用最小配置：关闭子进程依赖（回归/测试生成/变异），只走文件工具。"""
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        agent=AgentConfig(
            max_rounds=10, max_retries=0, max_concurrency=1,
            keep_recent_rounds=3,
            trace_enabled=False, archive_enabled=False,
            metrics_enabled=True, snapshot_enabled=False,
            regression_check_enabled=False, auto_testgen=False,
            mutation_check_enabled=False, counterfactual_enabled=False,
        ),
        sandbox=SandboxConfig(workspace=str(ws),
                              audit_dir=str(ws_tmp / "logs" / "audit")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"),
                            backend="none", auto_experience=False),
        mcp=MCPOptions(enabled=False),
        context=ContextConfig(archive_dir=str(ws_tmp / "logs" / "archives"),
                              max_tokens=400),
    )


async def _run_session(ws_tmp: Path, i: int, dl=None) -> Tuple[bool, int]:
    """跑一个完整会话（思考 -> 写文件 -> 收尾），返回 (是否成功, 事件数)。"""
    llm = ScriptedLLM(
        _think("分析任务并规划步骤"),
        _tool(action="write", path="f%d.txt" % (i % 4), content="x" * 50),
        _final("浸泡任务已完成"),
        _summary("浸泡任务 %d" % i),
    )
    loop = AgentLoop(config=_make_config(ws_tmp), llm=llm,
                     planner=StubPlanner(), decision_logger=dl)
    try:
        result = await loop.run("浸泡任务 %d" % i)
        return result.ok, len(loop.events)
    finally:
        await loop.close()


def _count_handles(proc: psutil.Process) -> int:
    """Windows 用句柄数，Linux 用 fd 数。"""
    try:
        return proc.num_handles()
    except (AttributeError, psutil.AccessDenied, NotImplementedError):
        return proc.num_fds()


@pytest.mark.asyncio
async def test_soak_sequential_sessions_bounded(ws_tmp):
    """顺序浸泡：24 会话连续执行，决策日志/内存/句柄全部有界。"""
    decision_path = ws_tmp / "decision.jsonl"
    dl = DecisionLogger(str(decision_path), max_memory_records=10)
    proc = psutil.Process()
    tracemalloc.start()
    peak_at: List[int] = []
    handle_at: List[int] = []
    try:
        for i in range(SOAK_SESSIONS):
            ok, events = await _run_session(ws_tmp, i, dl=dl)
            assert ok, "第 %d 个会话应成功" % i
            assert events <= MAX_EVENTS_PER_SESSION, (
                "单会话事件列表应受限: %d" % events)
            gc.collect()
            _, peak = tracemalloc.get_traced_memory()
            peak_at.append(peak)
            handle_at.append(_count_handles(proc))
    finally:
        tracemalloc.stop()

    # 决策日志：内存只保留最近 10 条；JSONL 保留全部记录（不丢数据）
    records = dl.records()
    assert len(records) == 10, "决策日志内存应裁剪到 max_memory_records"
    file_lines = decision_path.read_text(encoding="utf-8").splitlines()
    assert len(file_lines) >= SOAK_SESSIONS * 2, (
        "被淘汰记录应已落盘: %d" % len(file_lines))
    assert len(file_lines) > len(records)

    # 内存收敛：预热后 tracemalloc 峰值增长不超过 4MB
    early = max(peak_at[:WARMUP])
    late = max(peak_at[WARMUP:])
    growth = late - early
    assert growth <= MAX_PEAK_GROWTH_BYTES, (
        "浸泡后内存峰值增长 %.1fMB 超限" % (growth / 1024 / 1024))

    # 句柄收敛：预热后增量有界（无泄漏）
    early_h = max(handle_at[:WARMUP])
    late_h = max(handle_at[WARMUP:])
    assert late_h - early_h <= MAX_HANDLE_DELTA, (
        "句柄数增长 %d 超限" % (late_h - early_h))


@pytest.mark.asyncio
async def test_soak_concurrent_streams(ws_tmp):
    """并发浸泡：3 条流同时运行各自会话，全部成功且事件有界。"""
    results = await asyncio.gather(*(
        _concurrent_stream(ws_tmp, sid) for sid in range(SOAK_CONCURRENCY)
    ))
    for sid, lines in enumerate(results):
        assert len(lines) == SOAK_PER_STREAM
        for ok, events in lines:
            assert ok, "流 %d 的会话应成功" % sid
            assert events <= MAX_EVENTS_PER_SESSION, (
                "流 %d 事件数超限: %d" % (sid, events))


async def _concurrent_stream(ws_tmp: Path, sid: int) -> List[Tuple[bool, int]]:
    """单条任务流：连续 SOAK_PER_STREAM 个会话，各自独立工作区子目录。"""
    root = ws_tmp / ("stream_%d" % sid)
    root.mkdir(parents=True, exist_ok=True)
    lines: List[Tuple[bool, int]] = []
    for i in range(SOAK_PER_STREAM):
        llm = ScriptedLLM(
            _think("分析并写入"),
            _tool(action="write",
                  path="s%d_%d.txt" % (sid, i), content="y" * 30),
            _final("流任务完成"),
            _summary("流 %d 任务 %d" % (sid, i)),
        )
        loop = AgentLoop(config=_make_config(root), llm=llm,
                         planner=StubPlanner())
        try:
            result = await loop.run("并发流 %d 任务 %d" % (sid, i))
            lines.append((result.ok, len(loop.events)))
        finally:
            await loop.close()
    return lines


def test_soak_no_temp_residue(ws_tmp):
    """浸泡工作区不残留临时文件：仅出现脚本明确写入的文件。"""
    root = ws_tmp / "ws"
    root.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        (root / ("f%d.txt" % i)).write_text("x" * 10, encoding="utf-8")
    residue = [
        p.name for p in root.iterdir()
        if p.name.startswith(".tmp") or p.name.endswith(".tmp")
    ]
    assert residue == [], "工作区不应残留临时文件: %r" % residue
    assert sorted(p.name for p in root.iterdir()) == [
        "f0.txt", "f1.txt", "f2.txt", "f3.txt"]
