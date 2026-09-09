# -*- coding: utf-8 -*-
"""主线一 1.3C：会话状态显式生命周期测试。

覆盖：
- SessionState 数据模型：阶段推进 / 防线记录 / JSON 往返 / .agent_gate 落盘；
- 事件总线（agent/core/events.py）订阅与通知；
- phase-barrier defense_checks -> 防线状态映射（dual_review 失败 /
  human_review 命中抽样 -> WAITING_REVIEW + 复核命令提示）；
- AgentLoop 接线：gate 工具推进后 session_state 事件 / 会话快照 /
  收尾落盘；断点会话从 .agent_gate 恢复。
"""
import json

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          PhaseBarrierConfig, SandboxConfig)
from agent.core import events
from agent.core.loop import AgentLoop
from agent.core.session_state import (DefenseStatus, SessionState, Stage,
                                      record_defense_checks,
                                      session_state_path)
from agent.core.task import Task
from agent.llm import MockLLM


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=1,
                     criticality="critical")]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        if self._responses:
            return self._responses.pop(0)
        return '{"final_answer": "完成"}'  # 脚本耗尽兜底


def make_config(ws, enabled=False):
    return AppConfig(
        phase_barrier=PhaseBarrierConfig(
            enabled=enabled,
            workdir=str(ws),
            user_request="实现 fib",
        ),
        agent=AgentConfig(
            max_rounds=20, max_retries=1, max_concurrency=1,
            trace_enabled=False, archive_enabled=False,
            metrics_enabled=False, snapshot_enabled=False,
            regression_check_enabled=False, auto_testgen=False,
            mutation_check_enabled=False, counterfactual_enabled=False,
        ),
        sandbox=SandboxConfig(workspace=str(ws),
                              audit_dir=str(ws / "logs" / "audit")),
        memory=MemoryConfig(db_path=str(ws / "mem.db"),
                            backend="none", auto_experience=False),
        mcp=MCPOptions(enabled=False),
    )


def _write(rel, body):
    return json.dumps({"tool": "file_ops", "params": {
        "action": "write", "path": rel, "content": body}},
        ensure_ascii=False)


def _gate(action, stage=0):
    return json.dumps({"tool": "phase_barrier_gate", "params": {
        "action": action, "stage": stage}}, ensure_ascii=False)


def _run_tests():
    return json.dumps({"tool": "run_tests",
                       "params": {"framework": "pytest"}},
                      ensure_ascii=False)


def _final(text):
    return json.dumps({"final_answer": text}, ensure_ascii=False)


SPEC = """## 需求分析
实现一个计算斐波那契数列的函数 fib(n)，返回第 n 个斐波那契数，n 从 0 开始计数。
## 设计方案
采用迭代算法避免递归指数复杂度，处理 n=0/1 边界与负数输入。
## 接口定义
def fib(n: int) -> int
"""
TEST = """from fib import fib

def test_fib_base():
    assert fib(0) == 0
    assert fib(1) == 1

def test_fib_sequence():
    assert fib(5) == 5
    assert fib(10) == 55
"""
IMPL = """def fib(n: int) -> int:
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
"""


# ---------- 数据模型 ----------

def test_stage_labels_and_transition():
    state = SessionState()
    assert state.stage == Stage.REQUIREMENT
    assert state.transition(Stage.SPEC) is True
    assert state.stage.value == 1 and state.stage.label == "Spec 设计"
    assert state.transition("2") is True
    assert state.stage == Stage.TESTS
    assert state.transition(Stage.TESTS) is False  # 同阶段不重复记录
    assert any(e["kind"] == "transition" for e in state.recent_events)


def test_defense_recording_and_summary():
    state = SessionState()
    state.record_defense(1, DefenseStatus.PASSED)
    state.record_defense(2, DefenseStatus.FAILED, detail="spec 篡改约束")
    assert state.defense(1).status == DefenseStatus.PASSED
    assert state.defense(2).status == DefenseStatus.FAILED
    assert "双模型复核" in state.defense(2).label
    assert any("防线 2" in e["message"] for e in state.recent_events)


def test_json_round_trip_and_persistence(ws_tmp):
    state = SessionState(workspace=str(ws_tmp), prompt="持久化任务",
                         gate_enabled=True)
    state.record_defense(4, DefenseStatus.FAILED, detail="trace 越界")
    state.set_risk(72, {"complexity": 30})
    state.record_defense(5, DefenseStatus.WAITING_REVIEW,
                         detail="命中抽样", request_id="req_abc")
    state.transition(Stage.DELIVERY)
    saved = state.save()
    assert saved is not None and saved.exists()
    assert saved == session_state_path(ws_tmp)

    loaded = SessionState.load(ws_tmp)
    assert loaded is not None
    assert loaded.session_id == state.session_id
    assert loaded.stage == Stage.DELIVERY
    assert loaded.defense(2).status == DefenseStatus.NOT_TRIGGERED
    assert loaded.defense(4).status == DefenseStatus.FAILED
    assert loaded.defense(5).status == DefenseStatus.WAITING_REVIEW
    assert loaded.defense(5).request_id == "req_abc"
    assert loaded.risk_score == 72

    # to_dict / from_dict 独立往返
    clone = SessionState.from_dict(json.loads(state.to_json()))
    assert clone.to_dict() == state.to_dict()


# ---------- 防线证据映射 ----------

def test_defense_checks_mapping_fail_and_waiting():
    state = SessionState(workspace="ws_x")
    checks = [
        {"line": "requirement_template", "ok": True, "message": "模板完整"},
        {"line": "dual_review", "ok": False,
         "message": "反向篡改核查发现 spec 弱化密码长度约束",
         "evidence": {"kind": "tamper"}, "request_id": ""},
    ]
    outcome = record_defense_checks(state, checks, workspace="ws_x")
    assert state.defense(1).status == DefenseStatus.PASSED
    assert state.defense(2).status == DefenseStatus.FAILED
    assert "弱化密码长度" in state.defense(2).detail
    assert outcome["waiting_review"] is False

    # 防线 5 命中人工复核：WAITING_REVIEW + 风险分 + 复核命令提示
    checks2 = [
        {"line": "human_review", "ok": False,
         "message": "风险分 88/100 命中抽样",
         "evidence": {"risk_score": 88, "risk_breakdown": {"a": 1},
                      "sampled": True},
         "request_id": "req_xyz"},
    ]
    out2 = record_defense_checks(state, checks2, workspace="ws_x")
    assert state.defense(5).status == DefenseStatus.WAITING_REVIEW
    assert state.defense(5).request_id == "req_xyz"
    assert state.risk_score == 88
    assert out2["waiting_review"] is True
    assert "review-approve" in out2["review_hint"]
    assert "--workspace ws_x" in out2["review_hint"]

    # 人工已批准 -> ok=True 视为 PASSED
    checks3 = [{"line": "human_review", "ok": True,
                "message": "已获得人工批准", "request_id": "req_xyz"}]
    record_defense_checks(state, checks3, workspace="ws_x")
    assert state.defense(5).status == DefenseStatus.PASSED


# ---------- 事件总线 ----------

def test_session_events_bus():
    seen = []

    def cb(state):
        seen.append(state)

    try:
        events.subscribe(cb)
        state = SessionState()
        events.notify_tui(state)
        events.notify_tui(state)
        assert seen == [state, state]
        events.unsubscribe(cb)
        events.notify_tui(SessionState())
        assert len(seen) == 2
    finally:
        events.clear()


def test_session_events_clear():
    try:
        events.subscribe(lambda _s: None)
        events.clear()
        state = SessionState()
        events.notify_tui(state)  # 清空后通知不应抛错
    finally:
        events.clear()


# ---------- AgentLoop 接线 ----------

@pytest.mark.asyncio
async def test_loop_gate_syncs_session_state(ws_tmp):
    """Gate 流程推进时 loop 同步 SessionState 并落盘 .agent_gate。"""
    script = [
        _write("spec.md", SPEC),
        _gate("advance", 2),
        _write("test_fib.py", TEST),
        _gate("advance", 3),
        _write("fib.py", IMPL),
        _gate("advance", 4),
        _run_tests(),
        _gate("advance", 5),
        _final("fib 已实现"),
    ]
    loop = AgentLoop(config=make_config(ws_tmp, enabled=True),
                     llm=ScriptedLLM(*script), planner=StubPlanner())
    try:
        result = await loop.run("实现 fib")
    finally:
        await loop.close()
    assert result.ok

    state = loop.session_state
    assert state is not None
    assert state.gate_enabled is True
    assert state.finished is True and state.complete is True
    assert state.stage == Stage.DELIVERY

    # session_state 事件已广播（阶段/收尾）
    kinds = {e["data"].get("kind")
             for e in loop.events if e.get("type") == "session_state"}
    assert "finished" in kinds
    assert any("阶段" in str(e["data"].get("message", ""))
               for e in loop.events if e.get("type") == "session_state")

    # 快照落盘并可恢复
    snap = loop.session_snapshot()
    assert snap["session_id"] == state.session_id
    assert snap["session_state"]["stage"] == 6
    path = session_state_path(ws_tmp)
    assert path.exists()
    persisted = SessionState.load(ws_tmp)
    assert persisted is not None and persisted.stage == Stage.DELIVERY
    assert persisted.finished is True


@pytest.mark.asyncio
async def test_loop_restores_interrupted_session(ws_tmp):
    """未完成会话可从 .agent_gate 恢复（prompt 一致 + resume）。"""
    ws = str(ws_tmp)
    pre = SessionState(workspace=ws, prompt="断点任务", gate_enabled=True)
    pre.record_defense(1, DefenseStatus.PASSED)
    pre.record_defense(2, DefenseStatus.PASSED)
    pre.stage = Stage.TESTS
    pre.save()
    cfg = make_config(ws_tmp, enabled=False)  # 门禁关闭也可通过 resume 恢复会话
    loop = AgentLoop(config=cfg, llm=MockLLM(), planner=StubPlanner())
    try:
        state = loop._init_session_state("断点任务", resume=True)
    finally:
        await loop.close()
    assert state.session_id == pre.session_id
    assert state.stage == Stage.TESTS
    assert state.defense(2).status == DefenseStatus.PASSED
    assert loop._session_restored is True


@pytest.mark.asyncio
async def test_loop_disabled_session_still_created(ws_tmp):
    """门禁关闭时仍创建 SessionState（gate_enabled=False），快照为空结构。"""
    loop = AgentLoop(config=make_config(ws_tmp, enabled=False),
                     llm=MockLLM(), planner=StubPlanner())
    try:
        await loop.run("无门禁任务")
        snap = loop.session_snapshot()
        assert snap["enabled"] is False
        assert snap["session_state"] is not None
        assert snap["session_state"]["gate_enabled"] is False
        assert snap["session_state"]["stage"] == 0
    finally:
        await loop.close()
