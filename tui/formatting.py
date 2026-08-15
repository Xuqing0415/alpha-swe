"""事件格式化 —— 把 AgentLoop 事件渲染为纯终端风格日志行。

设计：`[HH:MM:SS] TYPE 内容`，TYPE 右对齐 5 列，8 种语义类型
（THINK/ACT/OBS/INFO/WARN/ERROR/OK/MEM），只用终端原生色，
不使用 emoji 与 256 色。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Tuple

from rich.text import Text

# 事件类型 -> (日志 TYPE, 颜色)
_LOG_TYPES: Dict[str, Tuple[str, str]] = {
    "think": ("THINK", "cyan"),
    "tool_call": ("ACT", "bold white"),
    "task_start": ("INFO", "bright_black"),
    "task_done": ("OK", "green"),
    "run_start": ("INFO", "bright_black"),
    "run_done": ("OK", "green"),
    "run_error": ("ERROR", "red"),
    "plan_created": ("INFO", "bright_black"),
    "task_interrupted": ("WARN", "yellow"),
    "task_preempted": ("PRQ", "yellow"),
    "task_resumed": ("INFO", "bright_black"),
    "priority_changed": ("INFO", "bright_black"),
    "interrupt": ("WARN", "yellow"),
    "skill_fallback": ("WARN", "yellow"),
    "skill_intervention": ("WARN", "yellow"),
    "skills_activated": ("INFO", "bright_black"),
    "mcp_tools": ("INFO", "bright_black"),
    "mcp_resources": ("INFO", "bright_black"),
    "testgen_generated": ("OK", "green"),
    "regression_clean": ("OK", "green"),
    "regression_detected": ("REG", "red"),
    "mutation_analyzed": ("MUT", "yellow"),
    "counterfactual_stored": ("MEM", "bright_black"),
    # 预留：记忆操作 / 观察结果
    "memory": ("MEM", "bright_black"),
    "obs": ("OBS", ""),
    "observation": ("OBS", ""),
}

_TYPE_WIDTH = 5


def format_event(record: Dict[str, Any]) -> Text:
    """把一条事件记录渲染成 `[HH:MM:SS] TYPE 内容` 的 Rich Text。"""
    etype = str(record.get("type", "unknown"))
    data = record.get("data", {}) or {}
    tag, color = _LOG_TYPES.get(etype, ("INFO", "bright_black"))
    text = Text()
    text.append(f"[{_timestamp(record)}] ", style="bright_black")
    text.append(f"{tag.rjust(_TYPE_WIDTH)} ", style=color or "")
    text.append(_format_body(etype, data))
    return text


def format_status_line(record: Dict[str, Any]) -> Text:
    """状态栏高亮行（run_done / run_error 用）。"""
    etype = record.get("type", "")
    if etype == "run_done":
        return Text(f"{record.get('data', {}).get('final_answer', '')}",
                    style="bold green")
    if etype == "run_error":
        return Text(f"{record.get('data', {}).get('error', '')}",
                    style="bold red")
    return format_event(record)


def _timestamp(record: Dict[str, Any]) -> str:
    ts = record.get("ts") or record.get("timestamp") or time.time()
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return time.strftime("%H:%M:%S")


def _format_body(etype: str, data: Dict[str, Any]) -> str:
    if etype == "run_start":
        return f"会话开始: {data.get('prompt', '')}"
    if etype == "plan_created":
        tasks = data.get("tasks", [])
        return f"规划 {data.get('total', len(tasks))} 个子任务: {_join(tasks)}"
    if etype == "task_start":
        base = f"任务 {data.get('task_id', '')}: {data.get('instruction', '')}"
        step = data.get("skill_step")
        if step:
            idx = (data.get("step_index") or 0) + 1
            total = data.get("step_total") or 0
            base += f" [技能 {data.get('skill', '')} {idx}/{total}: {step}]"
        return base
    if etype == "think":
        return _truncate(str(data.get("content", "")), 200)
    if etype == "tool_call":
        params = _compact_params(data.get("params"))
        status = "成功" if data.get("success") else "失败"
        output = _truncate(str(data.get("output", "")), 100)
        line = (f"{data.get('tool', '')} {params} -> {status}"
                + (f" | {output}" if output else ""))
        # 决策理由显式化（进阶 1.1）：与动作并列展示"为什么"
        reasoning = str(data.get("reasoning", "")).strip()
        if reasoning:
            line += f" | 理由: {_truncate(reasoning, 120)}"
        return line
    if etype == "task_done":
        return f"任务完成: {data.get('task_id', '')}"
    if etype == "task_interrupted":
        return f"任务被打断: {data.get('task_id', '')}"
    if etype == "task_preempted":
        return (f"任务被抢占暂停: {data.get('task_id', '')}"
                f"（priority={data.get('priority', '-')}）")
    if etype == "task_resumed":
        return (f"任务恢复执行: {data.get('task_id', '')}"
                f"（priority={data.get('priority', '-')}）")
    if etype == "priority_changed":
        return (f"优先级调整: 任务 {data.get('task_id', '')} -> "
                f"{data.get('priority', '')}")
    if etype == "interrupt":
        return f"注入指令: {data.get('prompt', '')}"
    if etype == "skill_fallback":
        return f"技能步骤回退: {data.get('step', data)}"
    if etype == "skill_intervention":
        return f"技能请求介入: {data.get('step', data)}"
    if etype == "skills_activated":
        skills = data.get("skills", [])
        return (f"技能工作流激活: {_join(skills)}"
                f"（展开 {data.get('total', 0)} 个子任务）")
    if etype == "mcp_tools":
        return f"MCP 合并 {data.get('count', 0)} 个工具: {_join(data.get('names', []))}"
    if etype == "mcp_resources":
        return f"MCP 注入 {data.get('count', 0)} 个资源"
    if etype == "run_done":
        phase = data.get("phase", "")
        return f"任务结束 [{phase}]: {data.get('final_answer', '')}"
    if etype == "run_error":
        return f"运行异常: {data.get('error', '')}"
    if etype == "testgen_generated":
        return (f"自动生成测试 {data.get('test_file', '')}"
                f"（模块 {data.get('module', '')}，"
                f"{data.get('targets', 0)} 个目标）")
    if etype == "regression_clean":
        return (f"回归检测通过: {data.get('module', '')} -> "
                f"{data.get('test_file', '')}")
    if etype == "regression_detected":
        return (f"回归检测失败: {data.get('module', '')} -> "
                f"{data.get('test_file', '')} | "
                f"{_truncate(str(data.get('summary', '')), 160)}")
    if etype == "mutation_analyzed":
        score = float(data.get("score", 0) or 0)
        survivors = _join(data.get("survivors", []))
        return (f"变异检测率 {score:.0%}（{data.get('killed', 0)}/"
                f"{data.get('total', 0)}）"
                f"存活变异: {survivors if survivors else '无'}")
    if etype == "counterfactual_stored":
        return (f"反事实教训{'写入' if data.get('stored') else '去重跳过'}: "
                f"归因={data.get('category', '')}，"
                f"转折点={_truncate(str(data.get('turning_point', '')), 100)}")
    return f"{etype}: {_truncate(str(data), 160)}"


def _compact_params(params: Any) -> str:
    if not params:
        return "{}"
    if isinstance(params, dict):
        items = []
        for k, v in list(params.items())[:4]:
            items.append(f"{k}={_truncate(str(v), 60)}")
        return "{" + ", ".join(items) + "}"
    return _truncate(str(params), 80)


def _join(items: Any, limit: int = 6) -> str:
    if not items:
        return ""
    parts = [str(x) for x in items]
    if len(parts) > limit:
        parts = parts[:limit] + [f"...共{len(items)}项"]
    return ", ".join(parts)


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", "\n ")
    return text if len(text) <= limit else text[:limit] + "..."


__all__ = ["format_event", "format_status_line"]
