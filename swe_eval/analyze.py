# -*- coding: utf-8 -*-
"""失败归因与报告渲染（方向二·阶段三）。

对未解决/失败实例按失败阶段分类：
- planning: 规划失败（任务拆分错误）
- retrieval: 检索失败（找不到相关代码）
- understanding: 理解失败
- modification: 修改失败（测试未通过）
- context: 上下文失败（关键信息丢失）
- tool: 工具失败（超时/输出截断）
- test: 测试执行失败
- budget/timeout: 资源预算或超时
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("swe_eval.analyze")

_CATEGORY_ORDER = ["planning", "retrieval", "understanding", "modification",
                   "context", "tool", "test", "budget", "timeout",
                   "unknown"]


def _category_of(result: Dict[str, Any]) -> str:
    status = result.get("status", "")
    if status == "timeout":
        return "timeout"
    if status == "budget":
        return "budget"
    adapter = result.get("adapter", {}) or {}
    payload = adapter.get("payload") or {}
    cat = payload.get("attribution", {}).get("category", "") or ""
    if cat and cat != "unknown":
        return cat
    if status == "unresolved":
        return "modification"
    if status == "error":
        return "tool"
    if status == "completed_no_eval":
        return "test"
    return cat or "unknown"


def classify_failures(results: List[Dict[str, Any]]) -> Dict[str, int]:
    """统计失败实例的分类计数（含 resolved 行，便于观察占比）。"""
    counter: Dict[str, int] = {c: 0 for c in _CATEGORY_ORDER}
    for r in results:
        if r.get("status") == "resolved":
            continue
        cat = _category_of(r)
        counter[cat] = counter.get(cat, 0) + 1
    return {k: v for k, v in counter.items() if v}


def failure_details(results: List[Dict[str, Any]],
                    top: int = 10) -> List[Dict[str, Any]]:
    """返回失败实例明细（错误信息 + 归因类别 + 指标）。"""
    rows = []
    for r in results:
        if r.get("status") == "resolved":
            continue
        adapter = r.get("adapter", {}) or {}
        rows.append({
            "instance_id": r.get("instance_id"),
            "status": r.get("status"),
            "category": _category_of(r),
            "error": (r.get("error") or adapter.get("error") or "")[:500],
            "tokens": adapter.get("tokens", 0),
            "elapsed_s": r.get("elapsed_s", 0),
            "eval_error": (r.get("eval", {}) or {}).get("error", "")[:500],
        })
    rows.sort(key=lambda x: (x["status"], x["instance_id"]))
    return rows[:top]


def render_markdown_report(report: Dict[str, Any],
                           results: List[Dict[str, Any]]) -> str:
    """渲染 Markdown 报告：总体指标 + 状态分布 + 失败分类 + 明细。"""
    s = report.get("summary", {})
    lines = ["# SWE-bench 评估报告", ""]
    lines.append(f"- 生成时间: {report.get('generated_at', '')}")
    lines.append(f"- 实例总数: {s.get('total', 0)}")
    lines.append(f"- 解决数: {s.get('resolved', 0)}")
    lines.append(f"- 解决率: {s.get('resolve_rate', 0) * 100:.1f}%")
    lines.append(f"- 平均耗时: {s.get('avg_elapsed_s', 0):.1f}s / 实例")
    lines.append(f"- 平均 token: {s.get('avg_tokens', 0):.0f}")
    lines.append(f"- 平均轮次: {s.get('avg_rounds', 0):.2f}")
    lines.append("")
    lines.append("## 状态分布")
    lines.append("")
    lines.append("| 状态 | 数量 |")
    lines.append("| --- | --- |")
    for k, v in sorted(s.get("status_counts", {}).items()):
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## 失败分类")
    lines.append("")
    for k, v in classify_failures(results).items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 失败实例明细")
    lines.append("")
    lines.append("| 实例 | 状态 | 分类 | 错误 |")
    lines.append("| --- | --- | --- | --- |")
    for d in failure_details(results, top=50):
        err = (d.get("error") or d.get("eval_error") or "-").replace(
            "|", "\\|")[:120]
        lines.append(f"| {d['instance_id']} | {d['status']} | "
                     f"{d['category']} | {err} |")
    lines.append("")
    return "\n".join(lines)


def save_markdown_report(report: Dict[str, Any], results: List[Dict[str, Any]],
                         results_dir: Path | str) -> Path:
    path = Path(results_dir) / "report.md"
    path.write_text(render_markdown_report(report, results), encoding="utf-8")
    return path
