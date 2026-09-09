"""会话状态 / 五道防线门禁视图（主线一 1.3C）。

把 ``AgentLoop.session_snapshot()`` 的快照渲染为纯终端风格面板
（无 emoji，信息密度优先，风格与 tui/app.py 一致）。

入口：``render_gate_panel(state_dict, *, enabled, available, template)``，
返回带 rich 标记的字符串，供 ``Static(markup=True)`` 直接更新。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from agent.core.session_state import DEFENSE_LINES, STAGE_LABELS

# 防线状态 -> (标记, 颜色)
_STATUS_MARK = {
    "not_triggered": ("--", "bright_black"),
    "passed": ("通过", "green"),
    "failed": ("失败", "red"),
    "waiting_review": ("复核", "yellow"),
}


def _fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "--:--:--"


def _status_cell(status: str, width: int = 3) -> str:
    mark, color = _STATUS_MARK.get(str(status or "not_triggered"),
                                   ("?", "bright_black"))
    mark = mark[:width].ljust(width)
    return f"[{color}]{mark}[/{color}]"


def _defense_rows(state: Optional[Dict[str, Any]]) -> List[str]:
    defenses = (state or {}).get("defenses") or {}
    rows: List[str] = []
    for line in sorted(DEFENSE_LINES):
        info = DEFENSE_LINES[line]
        record = defenses.get(str(line)) or {}
        status = str(record.get("status") or "not_triggered")
        name = str(record.get("name") or info["name"])
        label = info["label"]
        if status == "failed" and record.get("detail"):
            label += " " + str(record["detail"]).splitlines()[0][:46]
        rows.append(
            f"防线 {line} {label:<16} "
            f"{_status_cell(status)}"
        )
    return rows


def _event_lines(state: Optional[Dict[str, Any]], limit: int = 5) -> List[str]:
    events: List[Any] = (state or {}).get("recent_events") or []
    events = list(events)[-limit:]
    lines: List[str] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        msg = str(item.get("message") or "").replace("\n", " ")
        lines.append(f"  [{_fmt_ts(item.get('ts'))}] {msg[:88]}")
    return lines or ["  （暂无事件）"]


def render_gate_panel(
    state: Optional[Dict[str, Any]],
    *,
    enabled: bool,
    available: bool = False,
    template: str = "",
    restored: bool = False,
    max_events: int = 5,
) -> str:
    """渲染门禁面板（供 F5 主区「门禁」视图 / 测试断言使用）。"""
    lines: List[str] = []
    if not enabled:
        lines.append("[bold]门禁: 未启用[/bold] "
                     "（config/agent.yaml 的 phase_barrier.enabled 为 false）")
        lines.append("启用后在此展示 0-6 阶段与五道防线状态")
        return "\n".join(lines)
    if not available:
        lines.append("[bold yellow]门禁不可用（依赖缺失 / 初始化失败，降级放行）[/bold yellow]")
    if state is None:
        lines.append("（尚无会话状态）")
        return "\n".join(lines)

    stage = int(state.get("stage") or 0)
    stage_name = str(state.get("stage_name")
                     or STAGE_LABELS.get(stage, str(stage)))
    head = f"会话: {state.get('session_id', '-')}"
    if restored:
        head += "（已从 .agent_gate 恢复）"
    lines.append(head)
    lines.append(f"阶段: [bold]{stage_name}[/bold] ({stage}/7)"
                 f"{f'    模板: {template}' if template else ''}")
    status_bits: List[str] = []
    if state.get("finished"):
        status_bits.append("[green]已交付[/green]"
                           if state.get("complete")
                           else "[red]未交付[/red]")
    if state.get("error"):
        status_bits.append(f"错误: {str(state.get('error'))[:40]}")
    if status_bits:
        lines.append("状态: " + " | ".join(status_bits))
    lines.append("")
    lines.append("[bold]五道防线[/bold]")
    lines.extend(_defense_rows(state))
    risk = state.get("risk_score")
    risk_text = (f"{risk}/100" if isinstance(risk, int) and risk > 0
                 else "N/A")
    lines.append(f"风险评分: {risk_text}")
    review = (state.get("defenses") or {}).get("5") or {}
    if review.get("status") == "waiting_review":
        lines.append("[bold yellow]等待人工复核：请运行 "
                     "python -m anti_shortcut review-approve "
                     "--request-id {rid}[/bold yellow]".format(
                         rid=review.get("request_id") or ""))
    lines.append("")
    lines.append("[bold]最近事件[/bold]")
    lines.extend(_event_lines(state, limit=max_events))
    return "\n".join(lines)


def gate_stage_short(state: Optional[Dict[str, Any]]) -> str:
    """状态栏摘要，如 ``门禁 2/7 测试编写``（未启用返回空）。"""
    if not state:
        return ""
    stage = int(state.get("stage") or 0)
    return f"门禁 {stage}/7 {state.get('stage_name', '')}"


__all__ = ["gate_stage_short", "render_gate_panel"]
