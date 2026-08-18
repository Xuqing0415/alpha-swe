# -*- coding: utf-8 -*-
"""收敛期 P2：长时间稳定性（阶段一 1.2）+ 失败归因分析（阶段二 2.2）。

覆盖：
- 日志轮转：make_rotating_file_handler 小阈值触发 debug.log.1 / debug.log.2；
- 决策日志有界内存：超过 max_memory_records 后内存只保留最近 N 条，
  更早的记录已落盘 JSONL（文件行数保持完整，不丢数据）；
- Tracer 有界内存：超过 max_memory_spans 后已结束 span 自动落盘，
  活跃 span 保留在内存；
- 失败归因：classify_failure 各分支（记忆/上下文/工具/规划/检索/测试/理解/未知）；
- 聚合分析：aggregate_failures 只统计失败会话并输出 high_frequency；
- CLI 集成：失败任务的 JSON 输出附带 attribution 字段；
- 会话复盘：analyze_session.py 对失败档案输出归因。

运行：python -X utf8 -m pytest tests/test_p2_stability_attribution.py -q
"""
import argparse
import asyncio
import json
import logging
import runpy
import sys
from pathlib import Path

import pytest

from agent.attribution import (
    aggregate_failures,
    classify_failure,
    classify_session_failure,
)
from agent.config import AppConfig, load_config
from agent.core.decision_logger import DecisionLogger
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.observability.logging_setup import make_rotating_file_handler
from agent.observability.trace import Tracer


# ---- 日志轮转（阶段一 1.2）----

def test_rotating_file_handler_rotates(ws_tmp):
    log_path = ws_tmp / "logs" / "debug.log"
    handler = make_rotating_file_handler(
        str(log_path), max_bytes=512, backup_count=2)
    logger = logging.getLogger("p2_rotate_" + log_path.parent.name)
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    logger.propagate = False
    for i in range(300):
        logger.info("padding line %03d %s", i, "x" * 40)
    handler.flush()
    handler.close()
    files = sorted(p.name for p in log_path.parent.iterdir())
    assert "debug.log" in files
    assert any(f.startswith("debug.log.") for f in files), files


def test_decision_logger_bounded_memory_keeps_file(ws_tmp):
    log_path = ws_tmp / "decision.jsonl"
    dl = DecisionLogger(str(log_path), max_memory_records=5)
    for i in range(20):
        dl.record("name_%d" % i, "cfg.key", i, "决策 %d" % i)
    records = dl.records()
    assert len(records) == 5, "内存应只保留最近 5 条"
    assert records[-1]["name"] == "name_19"
    assert records[0]["name"] == "name_15"
    # 落盘文件保留全部 20 条（旧记录仅移出内存，不丢数据）
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20


def test_decision_logger_no_trim_without_log_path():
    dl = DecisionLogger(log_path=None, max_memory_records=5)
    for i in range(20):
        dl.record("n", "k", i, "d")
    assert len(dl.records()) == 20, "未配置落盘时不裁剪内存"


def test_tracer_bounded_memory_flushes_ended_spans(ws_tmp):
    tracer = Tracer(trace_dir=str(ws_tmp / "traces"),
                    max_memory_spans=5)
    for i in range(12):
        sp = tracer.start_span("span_%d" % i, kind="tool")
        sp.end(status="ok")
    assert len(tracer.snapshot()) <= 5
    flushed = []
    for p in (ws_tmp / "traces").glob("trace_*.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            flushed.append(json.loads(line))
    names = {r["name"] for r in flushed}
    assert "span_0" in names, "最旧已结束 span 应落盘"
    assert "span_7" not in names, "最近 span 应留在内存"
    # 活跃 span 不被裁剪
    active = tracer.start_span("active", kind="run")
    mem_names = [s["name"] for s in tracer.snapshot()]
    assert "active" in mem_names
    tracer.end_span(active)


# ---- 失败归因分类（阶段二 2.2）----

def test_classify_memory_failure():
    attr = classify_failure(
        decisions=[{"name": "retrieval_error", "timestamp": 1.0}])
    assert attr["category"] == "memory"
    assert attr["label"]
    assert attr["suggestions"]


def test_classify_premature_compression():
    events = [
        {"type": "think", "ts": 0.5, "data": {"content": "x"}},
        {"type": "tool_call", "ts": 1.0,
         "data": {"tool": "file_ops", "success": True}},
        {"type": "tool_call", "ts": 1.5,
         "data": {"tool": "file_ops", "success": True}},
    ]
    decisions = [{"name": "compression_level", "timestamp": 1.2}]
    attr = classify_failure(
        events=events, decisions=decisions,
        metrics={"counters": {"compressions": 1}})
    assert attr["category"] == "context"


def test_classify_tool_failure():
    events = [{"type": "tool_call", "data": {
        "tool": "terminal_execute", "success": False, "output": "boom"}}]
    attr = classify_failure(
        events=events, metrics={"counters": {"tool_failures": 1}})
    assert attr["category"] == "tool"


def test_classify_planning_fallback():
    attr = classify_failure(
        decisions=[{"name": "planner_fallback", "timestamp": 1.0}])
    assert attr["category"] == "planning"


def test_classify_retrieval_no_result():
    events = [{"type": "tool_call", "data": {
        "tool": "file_search", "success": True, "output": "未匹配到目标"}}]
    attr = classify_failure(events=events)
    assert attr["category"] == "retrieval"


def test_classify_testing_failure():
    events = [{"type": "tool_call", "data": {
        "tool": "run_tests", "success": False, "output": "2 failed"}}]
    attr = classify_failure(events=events)
    assert attr["category"] == "testing"


def test_classify_understanding_retries():
    attr = classify_failure(metrics={"counters": {"retries": 2}})
    assert attr["category"] == "understanding"


def test_classify_unknown():
    attr = classify_failure()
    assert attr["category"] == "unknown"
    assert attr["label"] == "未知"


# ---- 聚合分析 ----

def _fail_doc(session_id, prompt, decision_name, events=None):
    return {
        "schema": "alpha-swe-session-v1",
        "session_id": session_id,
        "prompt": prompt,
        "result": {"ok": False, "phase": "failed", "final_answer": ""},
        "events": events or [
            {"type": "run_done", "ts": 1.0, "data": {"phase": "failed"}}],
        "decisions": [{"name": decision_name, "timestamp": 1.0,
                       "config_key": "k", "config_value": "v",
                       "decision": "d"}],
        "metrics": {"counters": {}, "gauges": {}},
    }


def test_aggregate_failures_high_frequency():
    docs = [
        _fail_doc("s1", "任务A", "retrieval_error"),
        _fail_doc("s2", "任务B", "retrieval_error"),
        _fail_doc("s3", "任务C", "planner_fallback"),
        _fail_doc("s4", "任务D", "x", events=[{
            "type": "tool_call",
            "data": {"tool": "run_tests", "success": False}}]),
        _fail_doc("s5", "任务E", "x"),
    ]
    agg = aggregate_failures(docs)
    assert agg["total"] == 5
    assert agg["by_category"]["memory"] == 2
    assert agg["by_category"]["planning"] == 1
    assert agg["by_category"]["testing"] == 1
    assert agg["by_category"]["unknown"] == 1
    assert agg["high_frequency"][0]["category"] == "memory"
    assert agg["high_frequency"][0]["count"] == 2
    assert len(agg["items"]) == 5


def test_aggregate_failures_ignores_success():
    doc = _fail_doc("s6", "成功任务", "retrieval_error")
    doc["result"]["ok"] = True
    agg = aggregate_failures([doc])
    assert agg["total"] == 0


def test_classify_session_failure():
    doc = _fail_doc("s7", "任务", "planner_fallback")
    attr = classify_session_failure(doc)
    assert attr["category"] == "planning"


# ---- 端到端：AgentLoop 决策日志有界（浸泡冒烟）----

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
    return json.dumps({
        "problem": task, "solution": "完成",
        "steps": ["分析", "修改", "验证"], "key_files": ["a.txt"],
    }, ensure_ascii=False)


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


def _ws_path(p: Path) -> str:
    return str(p).replace("\\", "/")


def test_agentloop_bounded_decision_memory(ws_tmp):
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    cfg_path = ws_tmp / "agent.yaml"
    cfg_path.write_text("\n".join([
        "agent:",
        "  max_rounds: 10",
        "  max_retries: 2",
        "  max_concurrency: 1",
        "  keep_recent_rounds: 3",
        "  snapshot_enabled: false",
        "  trace_dir: %s" % _ws_path(ws_tmp / "logs" / "traces"),
        "  session_archive_dir: %s" % _ws_path(ws_tmp / "logs" / "sessions"),
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
        ""]), encoding="utf-8")

    decision_path = ws_tmp / "decision.jsonl"
    dl = DecisionLogger(str(decision_path), max_memory_records=10)
    llm = ScriptedLLM(
        _think("分析任务并规划步骤"),
        _tool(action="write", path="a.txt", content="aaa"),
        _final("已完成"),
        _summary("写入文件 a.txt"),
    )
    loop = AgentLoop(config=load_config(str(cfg_path)), llm=llm,
                     planner=StubPlanner(), decision_logger=dl)
    result = asyncio.run(_run_and_close(loop, "写入文件 a.txt"))
    assert result.ok

    records = loop._decision.records()
    assert len(records) <= 10, "长时间运行下决策日志内存应有界"
    file_lines = decision_path.read_text(encoding="utf-8").splitlines()
    assert file_lines, "决策 JSONL 应有内容"
    assert len(file_lines) > len(records), "被淘汰记录应已落盘（文件保留更多）"


# ---- CLI 集成：失败任务 JSON 输出归因 ----

def _mock_config(ws_tmp) -> str:
    ws = ws_tmp / "ws_cli"
    ws.mkdir(parents=True, exist_ok=True)
    return "\n".join([
        "agent:",
        "  max_rounds: 10",
        "  max_retries: 2",
        "  max_concurrency: 1",
        "  keep_recent_rounds: 3",
        "  snapshot_enabled: false",
        "  trace_dir: %s" % _ws_path(ws_tmp / "logs" / "traces"),
        "  session_archive_dir: %s" % _ws_path(ws_tmp / "logs" / "sessions"),
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
        "  max_tokens: 200",
        "  compression_threshold: 0.5",
        "  archive_dir: %s" % _ws_path(ws_tmp / "logs" / "archives"),
        "  output_truncate: 2000",
        ""])


def _cli_args(cfg_path, workspace, **over):
    from agent import __main__ as cli
    base = {
        "command": "run", "prompt": "触发解析失败", "config": str(cfg_path),
        "workspace": str(workspace), "output": "json", "timeout": None, "max_cost": None,
        "cost_per_1k_tokens": cli.DEFAULT_COST_PER_1K, "max_tokens": None,
        "disable_docker": True, "enable_mcp": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_cli_failure_payload_has_attribution(ws_tmp, capsys):
    from agent import __main__ as cli

    cfg_path = ws_tmp / "mock_agent.yaml"
    cfg_path.write_text(_mock_config(ws_tmp), encoding="utf-8")
    workspace = ws_tmp / "ws_cli"

    def factory(cfg: AppConfig) -> AgentLoop:
        llm = ScriptedLLM('{"hello": 1}', '{"world": 2}')
        return AgentLoop(config=cfg, llm=llm, planner=StubPlanner())

    args = _cli_args(cfg_path, workspace)
    exit_code = cli.run_cli(args, loop_factory=factory)
    assert exit_code == cli.EXIT_FAILED
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "attribution" in payload, "失败任务输出应附带归因"
    assert payload["attribution"]["category"] == "understanding"
    assert payload["attribution"]["suggestions"]


def test_cli_timeout_payload_attribution_unknown(ws_tmp, capsys):
    from agent import __main__ as cli

    cfg_path = ws_tmp / "mock_agent.yaml"
    cfg_path.write_text(_mock_config(ws_tmp), encoding="utf-8")

    class GatedLLM(MockLLM):
        def __init__(self):
            self.gate = asyncio.Event()

        async def complete(self, messages):
            await self.gate.wait()
            return '{"final_answer": "done"}'

    def factory(cfg: AppConfig) -> AgentLoop:
        return AgentLoop(config=cfg, llm=GatedLLM(), planner=StubPlanner())

    args = _cli_args(cfg_path, ws_tmp / "ws_cli", timeout=0.2)
    exit_code = cli.run_cli(args, loop_factory=factory)
    assert exit_code == cli.EXIT_TIMEOUT
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "attribution" in payload


# ---- 会话复盘脚本输出归因 ----

# ---- 归因规则修复回归：事件级失败信号 / 优先级 / 中断 ----

def _load_latest_archive(ws_tmp) -> dict:
    from agent.observability.archive import SessionArchive
    sessions = sorted((ws_tmp / "logs" / "sessions").glob("session_*.json"))
    assert sessions, "应生成会话档案"
    return SessionArchive.load(str(sessions[-1]))


def test_classify_degenerate_abort_via_events():
    """空参数保护只发 tool_call 失败事件、不计 metrics 时也应归工具失败。"""
    events = [
        {"type": "tool_call", "data": {
            "tool": "file_ops", "success": False,
            "params": {"action": "read"},
            "output": "[file_ops] 参数错误: 缺少必需参数 ['path']"}},
        {"type": "tool_call", "data": {
            "tool": "file_ops", "success": False,
            "params": {"action": "read"},
            "output": "[file_ops] 参数错误: 缺少必需参数 ['path']"}},
    ]
    attr = classify_failure(events=events, metrics={"counters": {}})
    assert attr["category"] == "tool", "事件级工具失败应归 tool 而非 unknown"


def test_classify_testing_beats_general_tool_counter():
    """测试失败信号优先于泛化 tool_failures 计数（避免 testing 分支不可达）。"""
    events = [{"type": "tool_call", "data": {
        "tool": "run_tests", "success": False, "output": "2 failed"}}]
    attr = classify_failure(
        events=events, metrics={"counters": {"tool_failures": 1}})
    assert attr["category"] == "testing"


def test_classify_retrieval_beats_tool_counter():
    """检索失败信号优先于泛化 tool_failures 计数。"""
    events = [{"type": "tool_call", "data": {
        "tool": "file_search", "success": False, "output": "搜索执行失败"}}]
    attr = classify_failure(
        events=events, metrics={"counters": {"tool_failures": 1}})
    assert attr["category"] == "retrieval"


def test_classify_interrupt():
    attr = classify_failure(metrics={"counters": {"interrupts": 2}})
    assert attr["category"] == "interrupt"
    assert attr["label"] == "用户中断"


def test_agentloop_degenerate_abort_counts_metrics(ws_tmp):
    """空参数连续 3 次触发保护性中止：metrics 计数、决策点与归因一致。"""
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    cfg_path = ws_tmp / "agent.yaml"
    cfg_path.write_text("\n".join([
        "agent:",
        "  max_rounds: 10",
        "  max_retries: 2",
        "  max_concurrency: 1",
        "  keep_recent_rounds: 3",
        "  snapshot_enabled: false",
        "  trace_dir: %s" % _ws_path(ws_tmp / "logs" / "traces"),
        "  session_archive_dir: %s" % _ws_path(ws_tmp / "logs" / "sessions"),
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
        ""]), encoding="utf-8")

    llm = ScriptedLLM(
        _think("尝试读取文件"),
        _tool(action="read"),   # 缺 path -> 空参数
        _tool(action="read"),
        _tool(action="read"),   # 第 3 次触发中止
    )
    loop = AgentLoop(config=load_config(str(cfg_path)), llm=llm,
                     planner=StubPlanner())
    result = asyncio.run(_run_and_close(loop, "读取文件"))
    assert result.ok is False

    snap = loop.metrics.snapshot()
    assert snap["counters"].get("tool_failures", 0) == 3, (
        "空参数保护应计入工具失败指标")
    assert any(d["name"] == "degenerate_abort"
               for d in loop._decision.records()), "应记录 degenerate_abort 决策点"

    doc = _load_latest_archive(ws_tmp)
    attr = classify_session_failure(doc)
    assert attr["category"] == "tool", "空参数中止档案应归因工具失败"


def test_analyze_session_script_attribution(ws_tmp, capsys, monkeypatch):
    from agent.observability.archive import SessionArchive

    arch = SessionArchive(str(ws_tmp / "sessions"), enabled=True)
    path = arch.write(
        "任务", [{"type": "run_done", "ts": 1.0,
                  "data": {"phase": "failed"}}],
        [], [{"name": "planner_fallback", "timestamp": 1.0,
              "config_key": "k", "config_value": "v", "decision": "d"}],
        {"counters": {}, "gauges": {}}, None)
    assert path is not None

    monkeypatch.setattr(sys, "argv", ["analyze_session.py", str(path), "--json"])
    with pytest.raises(SystemExit) as ei:
        runpy.run_path("scripts/analyze_session.py", run_name="__main__")
    assert ei.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["attribution"]["category"] == "planning"

    monkeypatch.setattr(sys, "argv",
                        ["analyze_session.py", str(path), "--text"])
    with pytest.raises(SystemExit) as ei2:
        runpy.run_path("scripts/analyze_session.py", run_name="__main__")
    assert ei2.value.code == 0
    text = capsys.readouterr().out
    assert "归因" in text
    assert "规划失败" in text
