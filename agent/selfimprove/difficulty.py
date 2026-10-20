# -*- coding: utf-8 -*-
"""任务难度 Critic（主线三 4.1）——基于实际执行过程给出 1~5 难度。

难度不采用 Planner 的先验猜测，而是任务完成后回看真实过程：
- 子任务数量 / 指令长度（任务规模）；
- ReAct 轮次与工具调用次数（执行复杂度）；
- 重试、失败、改动文件数（不确定性）；
- 是否暴露失败模式（失败任务通常更难）。

输出结构化评分与信号，供能力画像做难度加权（简单任务成功权重更低）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class DifficultyAssessment:
    """一次过程 Critic 评估结果。"""

    score: int
    signals: List[str] = field(default_factory=list)

    @property
    def rationale(self) -> str:
        return "；".join(self.signals) if self.signals else "常规任务"

    def to_dict(self) -> Dict[str, Any]:
        return {"score": self.score, "signals": list(self.signals),
                "rationale": self.rationale}


def _task_count(tasks: Optional[Iterable[Any]]) -> int:
    try:
        return len(list(tasks or []))
    except TypeError:
        return 0


def _event_stats(events: Optional[Iterable[Dict[str, Any]]]) -> Dict[str, int]:
    tools = failures = retries = writes = 0
    files = set()
    for event in events or []:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "tool_call":
            tools += 1
            if not data.get("success"):
                failures += 1
            if data.get("tool") == "file_ops":
                params = data.get("params") or {}
                if str(params.get("action", "")) in ("write", "edit", "append"):
                    writes += 1
                    path = str(params.get("path") or "")
                    if path:
                        files.add(path)
        elif kind in ("task_retry", "retry"):
            retries += 1
    return {"tools": tools, "failures": failures, "retries": retries,
            "writes": writes, "files": len(files)}


def assess_difficulty(prompt: str, result: Any) -> DifficultyAssessment:
    """按执行过程评估任务难度（1~5，确定性，不调用 LLM）。"""
    tasks = list(getattr(result, "tasks", None) or [])
    events = list(getattr(result, "events", None) or [])
    rounds = int(getattr(result, "total_rounds", 0) or 0)
    if rounds <= 0:
        rounds = sum(int(getattr(t, "round_count", 0) or 0) for t in tasks)
    stats = _event_stats(events)
    failed_tasks = sum(
        1 for t in tasks if str(getattr(t, "status", "")).endswith("failed"))
    failed_run = failed_tasks > 0 or str(
        getattr(result, "phase", "")).lower().endswith("failed")

    score = 1
    signals: List[str] = []
    if len(str(prompt or "")) >= 120 or _task_count(tasks) >= 2:
        score += 1
        signals.append(f"任务规模较大（{_task_count(tasks)} 个子任务）")
    if rounds >= 4 or stats["tools"] >= 8:
        score += 1
        signals.append(f"执行链较长（{rounds} 轮 / {stats['tools']} 次工具调用）")
    if stats["retries"] > 0 or stats["failures"] > 0 or failed_tasks > 0:
        score += 1
        signals.append(
            f"过程存在不确定性（重试 {stats['retries']}、失败工具 "
            f"{stats['failures']}、失败任务 {failed_tasks}）")
    if stats["files"] >= 3 or failed_run:
        score += 1
        signals.append(
            f"影响面较大（{stats['files']} 个文件）"
            if stats["files"] >= 3 else "任务未一次通过")
    return DifficultyAssessment(score=max(1, min(score, 5)), signals=signals)


__all__ = ["DifficultyAssessment", "assess_difficulty"]
