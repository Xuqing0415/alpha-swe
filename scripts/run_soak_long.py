# -*- coding: utf-8 -*-
"""长时间浸泡现场打卡工具（>8h 连续运行稳定性验证）。

在同一进程内连续运行大量 AgentLoop 会话（复用 LLM/规划器/决策日志），
周期性采样 RSS / 句柄数 / 单会话事件数，用线性回归判断是否泄漏：

- RSS 增长斜率 > max_rss_grow_mb_per_hour -> 判定泄漏，退出码 1；
- 句柄斜率 > max_handle_grow_per_hour -> 判定泄漏，退出码 1；
- 任意会话事件数超过 max_events_per_session -> 判定泄漏，退出码 1。

输出：
- <report_dir>/soak_heartbeat.jsonl  每分钟心跳（时间戳/RSS/句柄/会话数/斜率）
- <report_dir>/soak_report_<ts>.json 最终报告（含结论与斜率）

示例（CI 每日探针 / 本地打卡）:
    python -X utf8 scripts/run_soak_long.py --hours 1
    python -X utf8 scripts/run_soak_long.py --hours 12 --report-dir logs/soak/12h
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import psutil

# 允许直接以 `python scripts/run_soak_long.py` 运行（scripts/ 不在 sys.path）
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.config import (AgentConfig, AppConfig, ContextConfig, MCPOptions,
                          MemoryConfig, SandboxConfig)
from agent.core.decision_logger import DecisionLogger
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM

DEFAULT_SESSIONS_PER_BATCH = 12
WARMUP_SECONDS = 600            # 运行不足 10 分钟视为预热探测，不按回归判定泄漏
MIN_FIT_SAMPLES = 12            # 斜率回归所需最少采样点数
REGRESSION_WINDOW = 60          # 参与斜率回归的最近采样点数


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        if not self._responses:
            return '{"think": "无更多脚本响应，直接收尾", ' \
                   '"final_answer": "浸泡任务完成"}'
        return self._responses.pop(0)


def _think(text: str) -> str:
    return json.dumps({"think": text}, ensure_ascii=False)


def _tool(**params: Any) -> str:
    return json.dumps({"tool": "file_ops", "params": params},
                      ensure_ascii=False)


def _final(text: str) -> str:
    return json.dumps({"final_answer": text}, ensure_ascii=False)


def _count_handles(proc: psutil.Process) -> int:
    try:
        return proc.num_handles()
    except (AttributeError, psutil.AccessDenied, NotImplementedError):
        return proc.num_fds()


def _build_config(work_root: Path) -> AppConfig:
    ws = work_root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        agent=AgentConfig(
            max_rounds=8, max_retries=0, max_concurrency=1,
            keep_recent_rounds=3, max_events=50,
            trace_enabled=False, archive_enabled=False,
            metrics_enabled=True, snapshot_enabled=False,
            regression_check_enabled=False, auto_testgen=False,
            mutation_check_enabled=False, counterfactual_enabled=False,
            self_improve_enabled=False, state_tracker_enabled=False,
            workspace_context_enabled=False,
        ),
        sandbox=SandboxConfig(
            workspace=str(ws),
            audit_dir=str(work_root / "audit"),
            docker_enabled=False,
        ),
        memory=MemoryConfig(
            # 浸泡聚焦循环资源，用本地单层 sqlite（避免全局层写用户主目录）
            db_path=str(work_root / "mem.db"),
            backend="sqlite", auto_experience=False, layered=False,
        ),
        mcp=MCPOptions(enabled=False),
        context=ContextConfig(
            archive_dir=str(work_root / "archives"),
            max_tokens=400,
        ),
    )


def _linear_slope(points: List[Dict[str, float]]) -> float:
    """对 [(x_hours, y)] 做最小二乘，返回斜率（单位 y/小时）。"""
    n = len(points)
    if n < 2:
        return 0.0
    xs = [p["x"] for p in points]
    ys = [p["y"] for p in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def _linear_fit(points: List[Dict[str, float]],
                max_units_per_hour: float,
                label: str) -> Dict[str, Any]:
    slope = _linear_slope(points)
    ok = abs(slope) <= max_units_per_hour
    return {"label": label, "slope_per_hour": round(slope, 4),
            "max_per_hour": max_units_per_hour,
            "ok": bool(ok), "samples": len(points)}


async def _run_batch(cfg: AppConfig, dl: DecisionLogger,
                     base: int, count: int,
                     work_root: Path) -> List[int]:
    """连续跑 count 个会话，返回每个会话的事件数。"""
    event_counts: List[int] = []
    for i in range(count):
        idx = base + i
        llm = ScriptedLLM(
            _think("浸泡任务分析"),
            _tool(action="write",
                  path="f%d.txt" % (idx % 16), content="x" * 40),
            _final("浸泡任务完成"),
        )
        loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(),
                         decision_logger=dl)
        try:
            result = await loop.run("长时间浸泡任务 %d" % idx)
            if not result.ok:
                raise RuntimeError("会话 %d 失败: %s"
                                   % (idx, result.final_answer[:200]))
            event_counts.append(len(loop.events))
        finally:
            await loop.close()
    return event_counts

def _judge(warmed: bool, enough_samples: bool, bounded_events: bool,
           rss_fit: Dict[str, Any],
           handle_fit: Dict[str, Any]) -> Tuple[str, bool]:
    """判定浸泡结论：预热探测只报 warmup；够长且有足够采样才按回归判定。"""
    if warmed and enough_samples:
        verdict = ("ok" if bounded_events
                   and rss_fit["ok"] and handle_fit["ok"] else "fail")
    else:
        verdict = "warmup"
    ok = bool(bounded_events if verdict == "warmup"
              else verdict == "ok")
    return verdict, ok


def main() -> int:
    ap = argparse.ArgumentParser(description="长时间浸泡现场打卡")
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--sessions-per-batch", type=int,
                    default=DEFAULT_SESSIONS_PER_BATCH)
    ap.add_argument("--report-dir", default="logs/soak")
    ap.add_argument("--max-rss-grow-mb-per-hour", type=float, default=64.0)
    ap.add_argument("--max-handle-grow-per-hour", type=float, default=4000.0)
    ap.add_argument("--max-events-per-session", type=int, default=50)
    args = ap.parse_args()

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    work_root = report_dir / "work"
    cfg = _build_config(work_root)
    decision_path = report_dir / "decision.jsonl"
    dl = DecisionLogger(str(decision_path), max_memory_records=None)
    proc = psutil.Process()
    started = time.time()
    deadline = started + args.hours * 3600.0
    last_heartbeat = 0.0
    sessions = 0
    rss_points: List[Dict[str, float]] = []
    handle_points: List[Dict[str, float]] = []
    max_events = 0
    heartbeat: List[Dict[str, Any]] = []

    try:
        while True:
            counts = asyncio.run(_run_batch(
                cfg, dl, sessions, args.sessions_per_batch, work_root))
            sessions += len(counts)
            max_events = max(max_events, *counts) if counts else max_events
            elapsed_h = (time.time() - started) / 3600.0
            rss_mb = proc.memory_info().rss / 1024.0 / 1024.0
            handles = float(_count_handles(proc))
            rss_points.append({"x": elapsed_h, "y": rss_mb})
            handle_points.append({"x": elapsed_h, "y": handles})
            if len(rss_points) > REGRESSION_WINDOW:
                rss_points.pop(0)
                handle_points.pop(0)
            # 每小时心跳
            if time.time() - last_heartbeat >= 60 or sessions <= 1:
                last_heartbeat = time.time()
                row = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "elapsed_s": round(time.time() - started, 1),
                    "sessions": sessions,
                    "rss_mb": round(rss_mb, 2),
                    "handles": int(handles),
                    "max_events_per_session": max_events,
                }
                heartbeat.append(row)
                with open(report_dir / "soak_heartbeat.jsonl", "a",
                          encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(json.dumps(row, ensure_ascii=False), flush=True)
            if time.time() >= deadline:
                break
    except KeyboardInterrupt:
        print("浸泡被中断，保留已采样数据")
    finally:
        asyncio.run(_close_memory(cfg))

    elapsed = time.time() - started
    rss_fit = _linear_fit(rss_points, args.max_rss_grow_mb_per_hour, "rss_mb")
    handle_fit = _linear_fit(handle_points, args.max_handle_grow_per_hour,
                             "handles")
    bounded_events = max_events <= args.max_events_per_session
    warmed = elapsed >= WARMUP_SECONDS
    enough_samples = len(rss_points) >= MIN_FIT_SAMPLES
    verdict, ok = _judge(warmed, enough_samples, bounded_events,
                         rss_fit, handle_fit)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(elapsed, 1),
        "duration_hours": round(elapsed / 3600.0, 3),
        "sessions": sessions,
        "sessions_per_batch": args.sessions_per_batch,
        "max_events_per_session": max_events,
        "events_bounded": bool(bounded_events),
        "verdict": verdict,
        "warmup_reason": (
            None if verdict != "warmup" else
            ("run_short" if not warmed else "samples_insufficient")),
        "rss_fit": rss_fit,
        "handle_fit": handle_fit,
        "ok": bool(ok),
        "decision_log": str(decision_path),
    }
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = report_dir / ("soak_report_%s.json" % ts)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print("\n浸泡报告: %s" % out_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


async def _close_memory(cfg: AppConfig) -> None:
    try:
        from agent.memory.factory import build_memory
        mem = build_memory(cfg.memory)
        mem.close()
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
