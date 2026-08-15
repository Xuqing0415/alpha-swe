# -*- coding: utf-8 -*-
"""进阶 1.1 决策理由显式化——reasoning 字段的解析、强制校验、记录与展示。

覆盖：
- Parser 解析 tool/final_answer/think 中的 reasoning；
- require_reasoning 开启时缺失/过短的 reasoning 被拒绝并要求重试；
- AgentLoop 把 reasoning 记入决策日志（tool.reasoning / final.reasoning）、
  注入 tool_call/task_done 事件并写入会话档案；
- TUI format_event 在动作行并列展示"理由"。

运行：python -X utf8 -m pytest tests/test_reasoning.py -q
"""
import asyncio
import json
from pathlib import Path

from agent.config import load_config
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.observability.archive import SessionArchive
from agent.parser.parser import Parser
from tui.formatting import format_event


# ---- Parser：解析 reasoning ----

def test_parse_tool_call_reasoning():
    out = ('{"tool": "file_ops", "params": {"action": "read", "path": "a.py"}, '
           '"reasoning": "先读取文件确认当前实现"}')
    p = Parser().parse(out)
    assert p.action_type == "tool_call"
    assert p.reasoning == "先读取文件确认当前实现"


def test_parse_final_answer_reasoning():
    p = Parser().parse('{"final_answer": "完成", "reasoning": "测试已通过"}')
    assert p.action_type == "final_answer"
    assert p.reasoning == "测试已通过"


def test_parse_think_reasoning_kept():
    p = Parser().parse('{"think": "分析中", "reasoning": "先分析再动手"}')
    assert p.action_type == "think"
    assert p.reasoning == "先分析再动手"


def test_parse_without_reasoning_ok_by_default():
    p = Parser().parse('{"tool": "file_ops", "params": {"action": "read"}}')
    assert p.action_type == "tool_call"
    assert p.reasoning == ""


def test_require_reasoning_rejects_missing():
    p = Parser(require_reasoning=True)
    act = p.parse('{"tool": "file_ops", "params": {"action": "read"}}')
    assert act.action_type == "error"
    assert "reasoning" in act.error


def test_require_reasoning_rejects_too_short():
    p = Parser(require_reasoning=True)
    act = p.parse('{"tool": "file_ops", "params": {"action": "read"}, '
                  '"reasoning": "读"}')
    assert act.action_type == "error"


def test_require_reasoning_accepts_qualified():
    p = Parser(require_reasoning=True)
    out = ('{"tool": "file_ops", "params": {"action": "read", "path": "a.py"}, '
           '"reasoning": "先读取文件确认当前实现"}')
    assert p.parse(out).action_type == "tool_call"


def test_require_reasoning_ignores_think():
    p = Parser(require_reasoning=True)
    assert p.parse('{"think": "分析中"}').action_type == "think"


# ---- 端到端：AgentLoop 记录并落盘 reasoning ----

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


def _json(obj):
    return json.dumps(obj, ensure_ascii=False)


def _ws_path(p: Path) -> str:
    return str(p).replace("\\", "/")


async def _run_and_close(loop, prompt):
    try:
        return await loop.run(prompt)
    finally:
        try:
            await loop.close()
        finally:
            closer = getattr(loop.memory, "close", None)
            if closer:
                try:
                    closer()
                except Exception:
                    pass


def _write_config(ws_tmp, ws, require_reasoning: bool = False) -> Path:
    cfg = ws_tmp / "agent.yaml"
    lines = [
        "agent:",
        "  max_rounds: 10",
        "  max_retries: 2",
        "  max_concurrency: 1",
        "  keep_recent_rounds: 3",
        "  snapshot_enabled: false",
        "  trace_dir: %s" % _ws_path(ws_tmp / "logs" / "traces"),
        "  session_archive_dir: %s" % _ws_path(ws_tmp / "logs" / "sessions"),
    ]
    if require_reasoning:
        lines.append("  require_reasoning: true")
    lines += [
        "sandbox:",
        "  workspace: %s" % _ws_path(ws),
        "  docker_enabled: false",
        "memory:",
        "  backend: none",
        "llm:",
        "  provider: mock",
        "mcp:",
        "  enabled: false",
        "context:",
        "  max_tokens: 400",
        "  compression_threshold: 0.5",
        "  archive_dir: %s" % _ws_path(ws_tmp / "logs" / "archives"),
        "  output_truncate: 2000",
        "",
    ]
    cfg.write_text("\n".join(lines), encoding="utf-8")
    return cfg


def test_agentloop_records_reasoning(ws_tmp):
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    cfg_path = _write_config(ws_tmp, ws)

    llm = ScriptedLLM(
        _json({"think": "先分析任务"}),
        _json({"tool": "file_ops",
               "params": {"action": "read", "path": "a.txt"},
               "reasoning": "先读取文件确认当前内容"}),
        _json({"final_answer": "已读取",
               "reasoning": "读取成功且内容已确认"}),
    )
    loop = AgentLoop(config=load_config(str(cfg_path)), llm=llm,
                     planner=StubPlanner())
    result = asyncio.run(_run_and_close(loop, "读取 a.txt"))
    assert result.ok

    decisions = loop._decision.records()
    assert any(d["name"] == "tool.reasoning"
               and "先读取文件确认当前内容" in d["decision"]
               for d in decisions), "应记录 tool.reasoning 决策点"
    assert any(d["name"] == "final.reasoning"
               and "读取成功且内容已确认" in d["decision"]
               for d in decisions), "应记录 final.reasoning 决策点"

    tool_evts = [e for e in loop.events
                 if e.get("type") == "tool_call"
                 and e["data"].get("success")]
    assert tool_evts
    assert tool_evts[0]["data"].get("reasoning") == "先读取文件确认当前内容"
    done_evts = [e for e in loop.events if e.get("type") == "task_done"]
    assert done_evts and done_evts[0]["data"].get("reasoning") == "读取成功且内容已确认"

    sessions = sorted((ws_tmp / "logs" / "sessions").glob("session_*.json"))
    assert sessions, "应生成会话档案"
    doc = SessionArchive.load(str(sessions[-1]))
    assert any(d.get("name") == "tool.reasoning"
               for d in doc.get("decisions", [])), "档案应保留 tool.reasoning"
    assert any(e.get("data", {}).get("reasoning")
               for e in doc.get("events", [])
               if e.get("type") == "tool_call"), "档案事件应保留 reasoning"


def test_agentloop_require_reasoning_rejects_missing(ws_tmp):
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    cfg_path = _write_config(ws_tmp, ws, require_reasoning=True)

    llm = ScriptedLLM(
        _json({"think": "先分析任务"}),
        _json({"tool": "file_ops", "params": {"action": "read", "path": "a.txt"}}),
        _json({"tool": "file_ops",
               "params": {"action": "read", "path": "a.txt"},
               "reasoning": "先读取文件确认当前内容"}),
        _json({"final_answer": "已读取",
               "reasoning": "读取成功且内容已确认"}),
    )
    loop = AgentLoop(config=load_config(str(cfg_path)), llm=llm,
                     planner=StubPlanner())
    result = asyncio.run(_run_and_close(loop, "读取 a.txt"))
    assert result.ok, "缺失 reasoning 应触发重试而非失败"

    decisions = loop._decision.records()
    assert any(d["name"] == "tool.reasoning"
               for d in decisions), "重试后应成功记录 reasoning"
    assert loop.metrics.snapshot()["counters"].get("retries", 0) >= 1, (
        "缺失 reasoning 应计入解析重试")


# ---- TUI 展示 ----

def test_format_event_tool_call_shows_reasoning():
    line = format_event({
        "type": "tool_call",
        "data": {"tool": "file_ops", "success": True,
                 "params": {"action": "read", "path": "a.py"},
                 "reasoning": "先读取文件确认当前实现"},
    })
    plain = line.plain
    assert "理由:" in plain
    assert "先读取文件确认当前实现" in plain


def test_format_event_without_reasoning_unchanged():
    line = format_event({
        "type": "tool_call",
        "data": {"tool": "file_ops", "success": True,
                 "params": {"action": "read"}},
    })
    assert "理由:" not in line.plain
