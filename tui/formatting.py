"""事件格式化 —— 把 AgentLoop 事件渲染为 Rich Text（颜色区分），可单测。"""
from __future__ import annotations

from typing import Any, Dict

from rich.text import Text

# 事件 -> 展示标签/颜色
_EVENT_STYLE: Dict[str, str] = {
    "run_start": "bold white",
    "plan_created": "bold blue",
    "task_start": "bold cyan",
    "think": "cyan",
    "tool_call": "yellow",
    "task_done": "green",
    "task_interrupted": "magenta",
    "interrupt": "bold magenta",
    "mcp_tools": "dim blue",
    "mcp_resources": "dim blue",
    "run_done": "bold green",
    "run_error": "bold red",
}

def format_event(record: Dict[str, Any]) -> Text:
    """把一条事件记录渲染成单行/多行 Text（颜色区分，无图标前缀）。"""
    etype = record.get("type", "unknown")
    data = record.get("data", {})
    style = _EVENT_STYLE.get(etype, "white")
    return Text(_format_body(etype, data), style=style)


def _format_body(etype: str, data: Dict[str, Any]) -> str:
    if etype == "run_start":
        return f"任务开始: {data.get('prompt', '')}"
    if etype == "plan_created":
        tasks = data.get("tasks", [])
        return f"规划 {data.get('total', len(tasks))} 个子任务: {_join(tasks)}"
    if etype == "task_start":
        return f"任务 {data.get('task_id', '')}: {data.get('instruction', '')}"
    if etype == "think":
        return f"思考: {_truncate(data.get('content', ''), 160)}"
    if etype == "tool_call":
        params = _compact_params(data.get("params"))
        status = "成功" if data.get("success") else "失败"
        output = _truncate(str(data.get("output", "")), 120)
        return (f"调用 {data.get('tool', '')} {params} → {status}"
                + (f" | {output}" if output else ""))
    if etype == "task_done":
        return f"任务完成: {data.get('task_id', '')}"
    if etype == "task_interrupted":
        return f"任务被打断: {data.get('task_id', '')}"
    if etype == "interrupt":
        return f"用户注入高优先级指令: {data.get('prompt', '')}"
    if etype == "mcp_tools":
        return f"MCP 合并 {data.get('count', 0)} 个工具: {_join(data.get('names', []))}"
    if etype == "mcp_resources":
        return f"MCP 注入 {data.get('count', 0)} 个资源"
    if etype == "run_done":
        phase = data.get("phase", "")
        return f"任务结束 [{phase}]: {data.get('final_answer', '')}"
    if etype == "run_error":
        return f"运行异常: {data.get('error', '')}"
    return f"{etype}: {_truncate(str(data), 160)}"


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
    text = text.replace("\n", "\\n ")
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = ["format_event", "format_status_line"]
