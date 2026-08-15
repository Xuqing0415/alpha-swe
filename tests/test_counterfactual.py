# -*- coding: utf-8 -*-
"""进阶 1.2：反事实分析测试。

覆盖：归因类别/备选方案/转折点、长期记忆写入与去重、记忆禁用降级、
失败后自动记录反事实教训、相似任务检索命中时 Prompt 注入警告。
"""
import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.counterfactual import (analyze_failure, build_warning,
                                   format_lesson_text, prepend_warnings,
                                   store_lesson)
from agent.llm import MockLLM
from agent.memory.factory import build_memory
from agent.memory.store import NoopMemoryStore


def make_doc(events=None, decisions=None, metrics=None, **overrides):
    doc = {
        "schema": "alpha-swe-session-v1",
        "session_id": "cf-test-1",
        "prompt": "修复登录模块超时问题",
        "result": {"ok": False, "phase": "failed",
                   "final_answer": "任务失败", "total_rounds": 3},
        "events": events if events is not None else [
            {"type": "tool_call", "ts": 1.0,
             "data": {"tool": "file_ops", "success": False,
                      "params": {"action": "read", "path": "a.py"},
                      "error": "timeout after 60s"}},
        ],
        "decisions": decisions if decisions is not None else [],
        "metrics": metrics if metrics is not None
        else {"counters": {"tool_failures": 1}},
    }
    doc.update(overrides)
    return doc


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, criticality="critical")]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp, **mem_kw):
    return AppConfig(
        agent=AgentConfig(max_rounds=5, max_retries=2),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"), **mem_kw),
        mcp=MCPOptions(enabled=False),
    )


# ---- 分析：归因类别 / 备选方案 / 转折点 ----

def test_analyze_failure_tool_category():
    analysis = analyze_failure(make_doc())
    assert analysis["category"] == "tool"
    assert analysis["label"] == "工具失败"
    assert len(analysis["alternatives"]) == 3
    assert "file_ops" in analysis["turning_point"]
    lesson_text = format_lesson_text(analysis, "修复登录模块超时问题")
    assert "备选1" in lesson_text and "教训" in lesson_text


def test_analyze_failure_retrieval_and_planning():
    doc = make_doc(events=[
        {"type": "tool_call", "ts": 1.0,
         "data": {"tool": "search", "success": False,
                  "params": {"pattern": "getUserById"},
                  "error": "no results"}},
    ], metrics={"counters": {}})
    analysis = analyze_failure(doc)
    assert analysis["category"] == "retrieval"
    assert any("grep" in x["choice"] for x in analysis["alternatives"])

    doc2 = make_doc(events=[], decisions=[
        {"name": "planner_fallback", "config_key": "planner.enabled",
         "config_value": True, "decision": "LLM 规划失败回退单任务"},
    ], metrics={"counters": {}})
    analysis2 = analyze_failure(doc2)
    assert analysis2["category"] == "planning"
    assert analysis2["turning_point"].startswith("规划回退")
    assert any("调用图" in x["choice"] for x in analysis2["alternatives"])


def test_analyze_failure_reasoning_turning_point():
    doc = make_doc(
        events=[],
        decisions=[{"name": "tool.reasoning",
                    "config_key": "agent.require_reasoning",
                    "config_value": True,
                    "decision": "选择直接覆盖文件而未先读调用方"}],
        metrics={"counters": {}},
    )
    analysis = analyze_failure(doc)
    assert analysis["turning_point"].startswith("关键决策")


# ---- 长期记忆：写入 / 去重 / 降级 / 警告 ----

def test_store_lesson_and_dedup(ws_tmp):
    store = build_memory(MemoryConfig(backend="sqlite",
                                      db_path=str(ws_tmp / "mem.db")))
    try:
        analysis = analyze_failure(make_doc())
        assert store_lesson(store, analysis,
                            prompt="修复登录模块超时问题") is True
        assert store_lesson(store, analysis,
                            prompt="修复登录模块超时问题") is False
        hits = store.search("修复登录模块超时问题",
                            kinds=["counterfactual"])
        assert len(hits) == 1
        assert hits[0]["metadata"]["category"] == "tool"
        assert hits[0]["metadata"]["counterfactual"] is True
        assert hits[0]["metadata"]["negative"] is True
        assert hits[0]["metadata"]["lesson_key"]
    finally:
        store.close()


def test_store_lesson_disabled_memory():
    store = NoopMemoryStore()
    assert store_lesson(store, analyze_failure(make_doc()), prompt="x") is False


def test_prepend_warnings():
    hits = [
        {"kind": "counterfactual", "text": "当时选择 X 失败，建议 Y",
         "metadata": {"category": "tool", "counterfactual": True}},
        {"kind": "experience", "text": "正常经验", "metadata": {}},
    ]
    text, count = prepend_warnings(hits, "经验正文")
    assert count == 1
    assert text.startswith("[反事实警告·tool]")
    assert "经验正文" in text
    assert build_warning(hits[:1]) ==         "[反事实警告·tool] 当时选择 X 失败，建议 Y"
    text2, count2 = prepend_warnings([hits[1]], "正文")
    assert count2 == 0 and text2 == "正文"


# ---- loop 集成：失败自动记录 / 成功跳过 / 相似任务注入警告 ----

@pytest.mark.asyncio
async def test_loop_failure_stores_counterfactual(ws_tmp):
    cfg = make_config(ws_tmp, backend="sqlite")
    llm = ScriptedLLM('{"hello": 1}', '{"world": 2}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("解析持续失败")
    assert result.ok is False
    names = {d["name"] for d in loop._decision.records()}
    assert "counterfactual.stored" in names
    hits = loop.memory.search("解析持续失败", kinds=["counterfactual"])
    assert hits and hits[0]["metadata"]["category"] == "understanding"
    assert hits[0]["metadata"]["lesson_key"]


@pytest.mark.asyncio
async def test_loop_success_skips_counterfactual(ws_tmp):
    cfg = make_config(ws_tmp, backend="sqlite")
    llm = ScriptedLLM('{"final_answer": "完成"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("简单任务")
    assert result.ok
    names = {d["name"] for d in loop._decision.records()}
    assert "counterfactual.stored" not in names


@pytest.mark.asyncio
async def test_loop_injects_counterfactual_warning(ws_tmp):
    cfg = make_config(ws_tmp, backend="sqlite")
    store = build_memory(cfg.memory)
    try:
        store_lesson(store, analyze_failure(make_doc()),
                     prompt="修复登录模块超时问题")
        llm = ScriptedLLM('{"final_answer": "完成"}')
        loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner(),
                         memory=store)
        result = await loop.run("修复登录模块超时问题")
        assert result.ok
        names = {d["name"] for d in loop._decision.records()}
        assert "counterfactual.injected" in names
    finally:
        store.close()
