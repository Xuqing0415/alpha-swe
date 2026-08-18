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
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    cat = str(adapter.get("attribution") or
               (payload.get("attribution") or {}).get("category", "") or "")
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
    counter: Dict[str, int] = dict.fromkeys(_CATEGORY_ORDER, 0)
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


# ---- 方向一 2.1：轨迹信号与失败案例库 ----
def trajectory_signals(result: Dict[str, Any]) -> Dict[str, Any]:
    """从结果记录中提取轨迹信号，辅助失败归因与案例库检索。"""
    adapter = result.get("adapter", {}) or {}
    return {
        "llm_calls": int(adapter.get("llm_calls", 0) or 0),
        "rounds": int(adapter.get("rounds", 0) or 0),
        "tokens": int(adapter.get("tokens", 0) or 0),
        "files_modified": list(adapter.get("files_modified") or []),
        "attribution": str(adapter.get("attribution") or ""),
        "has_error": bool(adapter.get("error")),
    }


def refine_category(result: Dict[str, Any]) -> str:
    """在 status 启发式之外用轨迹信号修正归因类别。"""
    base = _category_of(result)
    adapter = result.get("adapter", {}) or {}
    files = adapter.get("files_modified") or []
    if base in ("unknown", "modification") and not files:
        # 有轮次但始终没改到文件 -> 检索/定位失败
        if int(adapter.get("rounds", 0) or 0) > 0:
            return "retrieval"
    if base == "modification" and files:
        eval_info = result.get("eval", {}) or {}
        if eval_info.get("resolved") is False and eval_info.get("error"):
            return "test"
    return base


def export_case_library(results: List[Dict[str, Any]],
                        results_dir: Path | str,
                        top: Optional[int] = None) -> Path:
    """导出失败案例库（JSON + Markdown），供人工复盘与瓶颈分析。"""
    results_dir = Path(results_dir)
    cases: List[Dict[str, Any]] = []
    for r in results:
        if r.get("status") == "resolved":
            continue
        adapter = r.get("adapter", {}) or {}
        cases.append({
            "instance_id": r.get("instance_id"),
            "repo": r.get("repo", ""),
            "status": r.get("status"),
            "category": refine_category(r),
            "signals": trajectory_signals(r),
            "error": (r.get("error") or adapter.get("error") or "")[:500],
            "eval_error": ((r.get("eval") or {}).get("error") or "")[:500],
            "patch_path": str(
                results_dir / str(r.get("instance_id")) / "patch.diff"),
        })
    if top:
        cases = cases[:top]
    json_path = results_dir / "case_library.json"
    json_path.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# 失败案例库", "", f"- 案例数: {len(cases)}", ""]
    for c in cases:
        sig = c["signals"]
        md.append(f"## {c['instance_id']} ({c['category']})")
        md.append(f"- 状态: {c['status']}  错误: {(c['error'] or '-')[:120]}")
        md.append(f"- 信号: llm_calls={sig['llm_calls']} rounds={sig['rounds']} "
                  f"files={sig['files_modified']}")
        md.append(f"- patch: `{c['patch_path']}`")
        md.append("")
    md_path = results_dir / "case_library.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    return json_path
