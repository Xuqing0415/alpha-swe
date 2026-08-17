# -*- coding: utf-8 -*-
"""主线三：自我评估与持续进化（3.1 能力画像 / 3.2 改进提议 / 3.3 基准提取 + 集成）。"""
import json
from types import SimpleNamespace

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.selfimprove import (BenchmarkExtractor, CapabilityProfile,
                               ProposalStore, STATUS_PROMOTED,
                               STATUS_REJECTED)


# ---- 3.1 能力画像 ----

def test_capability_record_updates_score(ws_tmp):
    prof = CapabilityProfile(path=str(ws_tmp / "capability.json"))
    for _ in range(3):
        dims = prof.record("修复登录模块空指针崩溃", ok=True)
    assert "debug" in dims and "code_modify" in dims
    prof.record("修复登录模块空指针崩溃", ok=False)
    assert 0.0 < prof.score("debug") < 1.0
    prof.close()


def test_capability_profile_text_highlights_weak(ws_tmp):
    prof = CapabilityProfile(path=str(ws_tmp / "capability.json"))
    for _ in range(4):
        prof.record("修复崩溃并定位根因", ok=False)
    text = prof.profile_text()
    assert "[能力画像]" in text
    assert "调试定位偏弱" in text
    prof.close()


def test_capability_trend_warning_after_decline(ws_tmp):
    prof = CapabilityProfile(path=str(ws_tmp / "capability.json"))
    for _ in range(6):
        prof.record("修复缓存性能问题", ok=True)
    for _ in range(5):
        prof.record("修复缓存性能问题", ok=False)
    warns = prof.trend_warnings()
    assert warns, "连续失败后应给出能力下降告警"
    assert any("性能优化" in w for w in warns)
    prof.close()


def test_capability_persists_across_reopen(ws_tmp):
    path = str(ws_tmp / "capability.json")
    prof = CapabilityProfile(path=path)
    prof.record("为模块编写测试", ok=True)
    prof.close()
    reopened = CapabilityProfile(path=path)
    assert reopened.score("test_writing") > 0.0
    reopened.close()


# ---- 3.2 改进提议 ----

def test_proposal_create_and_match(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"))
    pid = store.create_or_bump("planning", "修复空指针崩溃", "增强规划器")
    assert pid in [p["id"] for p in store.list()]
    assert store.match("修复登录空指针崩溃") == [pid]
    store.close()


def test_proposal_promoted_after_three_successes(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"))
    pid = store.create_or_bump("tool", "部署任务超时", "增强超时管控")
    for _ in range(2):
        assert store.verify(pid, ok=True) == "pending"
    assert store.verify(pid, ok=True) == STATUS_PROMOTED
    assert store.list(status=STATUS_PROMOTED)
    store.close()


def test_proposal_rejected_after_application_ceiling(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"), reject_after=5)
    pid = store.create_or_bump("retrieval", "找不到符号定义", "增强代码搜索")
    for _ in range(4):
        store.verify(pid, ok=False)
    assert store.verify(pid, ok=False) == STATUS_REJECTED
    store.close()


def test_proposal_user_reject(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"))
    pid = store.create_or_bump("context", "长任务信息丢失", "优化压缩策略")
    assert store.reject(pid) is True
    assert store.list(status=STATUS_REJECTED)
    store.close()


# ---- 3.3 基准集提取 ----

def _result(events, tasks, phase="completed"):
    return SimpleNamespace(events=events, tasks=tasks, phase=phase,
                           final_answer="ok")


def test_benchmark_extract_representative_task(ws_tmp):
    store = BenchmarkExtractor(path=str(ws_tmp / "bench.json"))
    events = [{"type": "tool_call", "data": {"success": True, "tool": "file_ops",
              "params": {"action": "write", "path": "a.py"}}}]
    tasks = [SimpleNamespace(id="s0"), SimpleNamespace(id="s1")]
    entry = store.evaluate("实现登录接口并补充测试", _result(events, tasks))
    assert entry and not entry.get("duplicate")
    assert entry["score"] >= 0.6
    assert store.entries(status="pending")
    store.close()


def test_benchmark_dedup_on_same_instruction(ws_tmp):
    store = BenchmarkExtractor(path=str(ws_tmp / "bench.json"))
    events = [{"type": "tool_call", "data": {"success": True, "tool": "file_ops",
              "params": {"action": "edit", "path": "a.py"}}}]
    tasks = [SimpleNamespace(id="s0"), SimpleNamespace(id="s1")]
    first = store.evaluate("重构数据层接口", _result(events, tasks))
    second = store.evaluate("重构数据层接口", _result(events, tasks))
    assert first and second
    assert second["duplicate"] is True
    assert len(store.entries()) == 1
    store.close()


def test_benchmark_confirm_and_reject(ws_tmp):
    store = BenchmarkExtractor(path=str(ws_tmp / "bench.json"))
    events = [{"type": "tool_call", "data": {"success": True, "tool": "file_ops",
              "params": {"action": "append", "path": "a.py"}}}]
    tasks = [SimpleNamespace(id="s0"), SimpleNamespace(id="s1")]
    entry = store.evaluate("新增 API 文档说明", _result(events, tasks))
    assert store.confirm(entry["id"]) is True
    assert store.entries(status="confirmed")
    store.close()


def test_benchmark_trend_warning(ws_tmp):
    prof = CapabilityProfile(path=str(ws_tmp / "cap.json"))
    store = BenchmarkExtractor(path=str(ws_tmp / "bench.json"), profile=prof)
    for _ in range(6):
        prof.record("修复空指针崩溃", ok=True)
    store.update_baseline()
    for _ in range(5):
        prof.record("修复空指针崩溃", ok=False)
    warns = store.trend_warnings()
    assert warns and any("调试定位" in w for w in warns)
    store.close()
    prof.close()


# ---- 集成：AgentLoop 会话结束后画像/提议自动更新 ----

def _make_config(ws_tmp):
    return AppConfig(
        agent=AgentConfig(max_rounds=6, max_retries=1, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(backend="sqlite", db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


class StubPlanner:
    async def plan(self, prompt, context="", call_graph=None,
                   project_context="", capability_profile=""):
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


def test_loop_failure_registers_proposal_and_capability(ws_tmp):
    cfg = _make_config(ws_tmp)
    llm = MockLLM(responder=lambda msgs: "这不是合法输出")
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())

    async def run():
        try:
            return await loop.run("修复登录模块空指针崩溃")
        finally:
            await loop.close()

    import asyncio
    result = asyncio.run(run())
    assert result.phase.value == "failed"
    names = [d["name"] for d in loop._decision.records()]
    assert "capability.updated" in names
    assert "selflearn.proposal" in names, "失败任务应登记改进提议"
    assert loop.proposals is not None and loop.proposals.list(
        status="pending")
    prof = loop.capability
    assert prof is not None, "能力画像应被装配"
    assert "debug" in prof.summary(), "失败任务应记录调试维度"
    assert prof.score("debug") == 0.0, "单次失败的成功率应为 0"
