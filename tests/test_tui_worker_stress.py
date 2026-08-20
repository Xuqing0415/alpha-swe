# -*- coding: utf-8 -*-
"""TUI worker 高事件率压测基准（收敛期 P1）。

覆盖三档压力：
1. 消息泵突发：测试侧直接 post_message 突发 N 个混合事件，测端到端
   吞吐（events/sec）并断言零丢失；
2. worker 全链路突发：BurstRunner 在 Textual worker 内以订阅回调方式
   高速发事件，模拟真实 AgentLoop 高频事件源；
3. 缓冲区有界：高事件率下 terminal 环形缓冲 / RichLog / VirtualLog
   均被裁剪，不无限增长。

所有场景同时断言：
- 事件零丢失（每个事件的唯一 token 都渲染进主日志）；
- refresh_status 事件驱动刷新被 _EVENT_REFRESH_MIN_INTERVAL 节流；
- worker 正常完成，无异常。

可通过环境变量调节规模：
  TUI_STRESS_DIRECT=1000  消息泵突发事件数
  TUI_STRESS_BURST=1200   worker 突发事件数
  TUI_STRESS_TERMINAL=2200  终端/变更缓冲区压力事件数

运行：python -X utf8 -m pytest tests/test_tui_worker_stress.py -q -s
"""
import asyncio
import os
import re
import time
from typing import Any, Dict, List

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentPhase, LoopResult
from agent.core.task import Task
from agent.llm import MockLLM
from tui.app import AlphaSWEApp
from tui.bridge import AgentRunner
from tui.messages import (AgentEventMessage, AgentFinishedMessage,
                          AgentStartedMessage)
from tui.vlog import VirtualLog

STRESS_DIRECT = int(os.environ.get("TUI_STRESS_DIRECT", "1000"))
STRESS_BURST = int(os.environ.get("TUI_STRESS_BURST", "1200"))
STRESS_TERMINAL = int(os.environ.get("TUI_STRESS_TERMINAL", "2200"))
_EVENT_REFRESH_MIN_INTERVAL = 0.1  # 与 tui/app.py 保持一致
_TERMINAL_MAX = 2000  # _append_terminal 环形缓冲上限
_DIFF_MAX = 1000  # diff-log RichLog 上限（与 terminal-log 一致）
_VLOG_MAX = 10000  # VirtualLog 环形缓冲上限

# 最新压测结果汇总（运行后打印）
RESULT: Dict[str, Any] = {}


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


def make_config(ws_tmp):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


def _token(i: int) -> str:
    return f"stress#{i}"


def _tool_call(i: int) -> Dict[str, Any]:
    return {
        "type": "tool_call",
        "data": {
            "tool": "file_ops",
            "params": {"action": "write", "path": f"src/{_token(i)}.py",
                       "content": "x"},
            "meta": {
                "path": f"src/{_token(i)}.py",
                "diff_before": "",
                "diff_after": f"def f{i}():\n    return {i}\n",
            },
            "success": True,
        },
    }


def _mixed_records(n: int) -> List[Dict[str, Any]]:
    """60% think + 40% tool_call（含 diff 渲染重路径）。"""
    recs: List[Dict[str, Any]] = []
    for i in range(n):
        if i % 5 < 3:
            recs.append({"type": "think",
                         "data": {"content": f"{_token(i)} 分析步骤"}})
        else:
            recs.append(_tool_call(i))
    return recs


def _tool_only_records(n: int) -> List[Dict[str, Any]]:
    return [_tool_call(i) for i in range(n)]


def _rendered_text(app) -> str:
    vlog = app.query_one("#main-log", VirtualLog)
    return "".join(str(line) for line in vlog.lines)


def _missing_tokens(rendered: str, n: int) -> List[str]:
    """返回未渲染的事件 token（用 \b 防止 stress#5 命中 stress#50）。"""
    missing = []
    for i in range(n):
        if not re.search(re.escape(_token(i)) + r"\b", rendered):
            missing.append(_token(i))
    return missing


async def _wait_until(pilot, predicate, timeout: float = 25.0,
                      interval: float = 0.02) -> bool:
    """轮询直到条件满足（pilot.pause 驱动 Textual 消息泵）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(interval)
    return predicate()


def _counting_refresh(orig):
    """包装 refresh_status 计数调用时间。"""

    def counting(self):
        self._stress_refresh_calls.append(time.monotonic())
        return orig(self)

    return counting


def _quiet_runner(app, prompt, cfg):
    """立即结束的 runner：用于纯消息泵突发测试。"""

    class QuietRunner(AgentRunner):
        async def run(self):
            self.app.post_message(AgentStartedMessage(prompt))
            res = LoopResult(final_answer="压测结束", phase=AgentPhase.COMPLETED)
            self.result = res
            self.app.post_message(AgentFinishedMessage(res))
            return res

    return QuietRunner(app, prompt, config=cfg, llm=MockLLM(),
                       planner=StubPlanner())


class BurstRunner(AgentRunner):
    """在 worker 内以订阅回调方式高速发事件的 runner。"""

    def __init__(self, app, prompt, records, *, cfg):
        super().__init__(app, prompt, config=cfg, llm=MockLLM(),
                         planner=StubPlanner())
        self.records = records
        self.emit_started: float = 0.0

    async def run(self):
        self.app.post_message(AgentStartedMessage(self.prompt))
        self.emit_started = time.monotonic()
        chunk = 64
        for i in range(0, len(self.records), chunk):
            for rec in self.records[i:i + chunk]:
                self._on_event(rec)
            await asyncio.sleep(0)  # 让消息泵排空，模拟真实环回
        res = LoopResult(final_answer="压测完成", phase=AgentPhase.COMPLETED)
        self.result = res
        self.app.post_message(AgentFinishedMessage(res))
        return res


def _assert_refresh_bounded(refresh_calls: List[float], elapsed: float, n: int):
    """N 个事件不应触发 N 次全量刷新；且不超过 时间窗/节流间隔 + 定时器余量。"""
    assert len(refresh_calls) < n, f"事件驱动刷新未被节流: {len(refresh_calls)}"
    bound = int(elapsed / _EVENT_REFRESH_MIN_INTERVAL) + 6
    assert len(refresh_calls) <= bound, (
        f"刷新次数超界: {len(refresh_calls)} > {bound} (elapsed={elapsed:.2f}s)")


@pytest.mark.asyncio
async def test_tui_worker_direct_burst_no_loss(ws_tmp, monkeypatch):
    """消息泵突发：N 个混合事件端到端渲染零丢失 + 刷新节流 + 吞吐基准。"""
    cfg = make_config(ws_tmp)
    n = STRESS_DIRECT
    records = _mixed_records(n)
    orig_refresh = AlphaSWEApp.refresh_status
    monkeypatch.setattr(AlphaSWEApp, "_stress_refresh_calls", [], raising=False)
    monkeypatch.setattr(AlphaSWEApp, "refresh_status",
                        _counting_refresh(orig_refresh))
    monkeypatch.setattr(AlphaSWEApp, "_make_runner",
                        lambda self: _quiet_runner(self, self.prompt, cfg))

    app = AlphaSWEApp("突发压测", config=cfg, llm=MockLLM(),
                      planner=StubPlanner())
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        t0 = time.monotonic()
        for rec in records:
            app.post_message(AgentEventMessage(rec))
        ok = await _wait_until(pilot, lambda: len(
            _missing_tokens(_rendered_text(app), n)) == 0)
        elapsed = time.monotonic() - t0
        missing = _missing_tokens(_rendered_text(app), n)
        assert ok and not missing, f"事件丢失 {len(missing)} 条: {missing[:5]}"
        rate = n / max(elapsed, 1e-6)
        calls = app._stress_refresh_calls
        _assert_refresh_bounded(calls, elapsed, n)
        assert getattr(app._finished, "ok", True)
        RESULT["direct_burst"] = {
            "events": n, "elapsed_s": round(elapsed, 3),
            "events_per_sec": round(rate, 1), "refresh_calls": len(calls),
        }


@pytest.mark.asyncio
async def test_tui_worker_burst_source_throughput(ws_tmp, monkeypatch):
    """worker 全链路突发：BurstRunner 高速发事件，完成且零丢失。"""
    cfg = make_config(ws_tmp)
    n = STRESS_BURST
    records = _mixed_records(n)
    orig_refresh = AlphaSWEApp.refresh_status
    monkeypatch.setattr(AlphaSWEApp, "_stress_refresh_calls", [], raising=False)
    monkeypatch.setattr(AlphaSWEApp, "refresh_status",
                        _counting_refresh(orig_refresh))

    def make_runner(self):
        return BurstRunner(self, self.prompt, records, cfg=cfg)

    monkeypatch.setattr(AlphaSWEApp, "_make_runner", make_runner)
    app = AlphaSWEApp("worker 突发压测", config=cfg, llm=MockLLM(),
                      planner=StubPlanner())
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        ok = await _wait_until(
            pilot,
            lambda: getattr(app, "_finished", None) is not None
            and len(_missing_tokens(_rendered_text(app), n)) == 0)
        # 从 worker 开始发射算起，覆盖 发射 + 消息泵排空 全窗口
        elapsed = time.monotonic() - app.runner.emit_started
        assert ok, "worker 未在超时内完成或事件未渲染完"
        assert getattr(app._finished, "ok", True)
        rate = n / max(elapsed, 1e-6)
        calls = app._stress_refresh_calls
        _assert_refresh_bounded(calls, elapsed, n)
        RESULT["burst_source"] = {
            "events": n, "elapsed_s": round(elapsed, 3),
            "events_per_sec": round(rate, 1), "refresh_calls": len(calls),
        }


@pytest.mark.asyncio
async def test_tui_worker_buffers_bounded(ws_tmp, monkeypatch):
    """缓冲区有界：高事件率下 terminal/diff/VirtualLog 均被裁剪。"""
    cfg = make_config(ws_tmp)
    n = STRESS_TERMINAL
    records = _tool_only_records(n)
    monkeypatch.setattr(AlphaSWEApp, "_make_runner",
                        lambda self: _quiet_runner(self, self.prompt, cfg))
    app = AlphaSWEApp("缓冲区压测", config=cfg, llm=MockLLM(),
                      planner=StubPlanner())
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        t0 = time.monotonic()
        for rec in records:
            app.post_message(AgentEventMessage(rec))
        ok = await _wait_until(
            pilot,
            lambda: app.query_one("#main-log", VirtualLog).row_count >= n)
        assert ok, "事件未被完整处理"
        elapsed = time.monotonic() - t0
        terminal_buf = len(app._terminal_lines)
        vlog_rows = app.query_one("#main-log", VirtualLog).row_count
        diff_buf = len(app._diff_buffer)
        diff_rows = len(app.query_one("#diff-log").lines)
        term_rows = len(app.query_one("#terminal-log").lines)
        # 隐藏期零直写：diff 缓冲有界，隐藏的 RichLog 不再累积 deferred renders
        assert diff_buf <= _DIFF_MAX + 100, f"diff 缓冲超限: {diff_buf}"
        assert diff_rows == 0, f"隐藏期 diff-log 出现直写: {diff_rows}"
        # 切到 diff 主区：从有界缓冲重放，RichLog 行数有界
        await pilot.press("f5")
        await pilot.pause(0.2)
        diff_rows = len(app.query_one("#diff-log").lines)
        assert 0 < diff_rows <= _DIFF_MAX + 100, f"diff 视图重放异常: {diff_rows}"
        RESULT["buffers"] = {
            "events": n, "elapsed_s": round(elapsed, 3),
            "terminal_lines": terminal_buf, "vlog_rows": vlog_rows,
            "diff_buf": diff_buf, "diff_rows_visible": diff_rows,
            "terminal_log_rows": term_rows,
        }
        assert terminal_buf <= _TERMINAL_MAX, f"terminal 缓冲超限: {terminal_buf}"
        assert vlog_rows <= _VLOG_MAX + 100, f"VirtualLog 超限: {vlog_rows}"
        assert term_rows <= 1000 + 100, f"terminal-log 未裁剪: {term_rows}"


def test_tui_worker_stress_summary():
    """汇总打印：pytest -s 运行时输出压测基准结果。"""
    if RESULT:
        print("\n==== TUI worker 高事件率压测基准 ====")
        for k, v in RESULT.items():
            print(f"  {k}: {v}")
