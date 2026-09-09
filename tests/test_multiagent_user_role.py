"""主线二 2.3：用户超级角色——否决原因结构化、黑板高优通道、决策里程碑标记。"""
import json
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.decision_logger import DecisionLogger
from agent.llm import MockLLM
from agent.multiagent import (Blackboard, MilestoneKind, OrchestratorAgent,
                              TeamPlanner, UserRole, UserMilestone, VetoReason,
                              WorkerAgent)
from agent.multiagent.messages import (USER_PRIORITY, USER_SENDER, Message,
                                       MsgType)
from agent.observability.archive import SessionReplay

PASS_VERDICT = '{"final_answer": "{\\"verdict\\": \\"pass\\", \\"suggestion\\": \\"\\"}"}'


def make_config(ws_tmp: Path) -> AppConfig:
    return AppConfig(
        agent=AgentConfig(max_rounds=8, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(backend="hybrid", db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


class ScriptedWorkerLLM(MockLLM):
    def __init__(self, *responses: str):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        system = messages[0].get("content", "") if messages else ""
        if "经验总结器" in system:
            return "{}"
        assert self._responses, "Worker LLM 脚本响应已耗尽"
        return self._responses.pop(0)


def make_workers(cfg, bb, coder_llm, reviewer_llm):
    from agent.config import load_team_config
    roles = {r.name: r for r in load_team_config().roles}
    return {
        "coder": WorkerAgent(roles["coder"], config=cfg, llm=coder_llm,
                             blackboard=bb),
        "reviewer": WorkerAgent(roles["reviewer"], config=cfg,
                                llm=reviewer_llm, blackboard=bb),
    }


def plan_llm_for(*items):
    return MockLLM(responder=lambda msgs: json.dumps(list(items),
                                                     ensure_ascii=False))


def make_orchestrator(cfg, bb, workers, plan_llm, dl=None):
    return OrchestratorAgent(
        config=cfg,
        blackboard=bb,
        workers=workers,
        planner=TeamPlanner(llm=plan_llm, roles=list(workers.keys())),
        max_review_retries=2,
        concurrency=1,
        decision_logger=dl,
    )


def make_role(ws_tmp, dl=None):
    bb = Blackboard()
    return bb, UserRole(blackboard=bb, decision_logger=dl or DecisionLogger())


# ---- 否决原因（2.3B） ----

def test_veto_reason_choices_labels_and_guidance():
    codes = [c["code"] for c in VetoReason.choices()]
    assert codes == ["direction", "method", "risk", "timing", "other"]
    labels = {c["code"]: c["label"] for c in VetoReason.choices()}
    assert "方向错误（重新理解任务）" in labels["direction"]
    assert "风险不可接受（需要更安全的做法）" in labels["risk"]
    assert "其他（手动输入）" in labels["other"]
    for reason in VetoReason:
        assert reason.guidance, f"{reason} 缺少行为指引"
    assert "不要用相同方法" in VetoReason.METHOD.guidance


def test_invalid_veto_reason_raises(ws_tmp):
    _, ur = make_role(ws_tmp)
    with pytest.raises(ValueError):
        ur.veto("task-1", "no-such-reason")


def test_veto_records_milestone_broadcast_and_decision(ws_tmp):
    bb, ur = make_role(ws_tmp)
    dl = ur.decision_logger
    m = ur.veto("task-9", "method", note="改走配置化方案")
    assert isinstance(m, UserMilestone)
    assert m.kind == MilestoneKind.VETO.value
    assert "task-9" in m.summary and "方案不当" in m.summary
    assert "不要用相同方法" in m.detail
    assert m.refs == ["task-9"]
    # 黑板用户通道：高优先级 USER_DECISION 广播
    msgs = bb.user_messages()
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.type == MsgType.USER_DECISION.value
    assert msg.sender == USER_SENDER
    assert msg.priority >= USER_PRIORITY
    assert msg.receiver == "*"
    assert msg.payload["reason"] == "method"
    assert "guidance" in msg.payload and msg.payload["note"] == "改走配置化方案"
    # 否决生效 + 决策日志里程碑
    assert ur.is_vetoed("task-9") and ur.veto_reason("task-9") == "method"
    names = [d.name for d in dl.decisions]
    assert names == ["user.milestone"]
    assert ur.milestones()[0]["human"] is True


def test_approve_clears_veto_and_records_milestone(ws_tmp):
    _, ur = make_role(ws_tmp)
    ur.veto("task-9", "risk")
    m = ur.approve("task-9", note="确认安全后可继续")
    assert m.kind == MilestoneKind.APPROVE.value
    assert not ur.is_vetoed("task-9")
    assert len(ur.milestones()) == 2
    kinds = [x["kind"] for x in ur.milestones()]
    assert kinds == ["veto", "approve"]


def test_reassign_requires_target_role(ws_tmp):
    _, ur = make_role(ws_tmp)
    with pytest.raises(ValueError):
        ur.reassign("task-9", "  ")
    m = ur.reassign("task-9", "tester", note="交给测试角色")
    assert m.kind == MilestoneKind.REASSIGN.value
    assert "tester" in m.summary
    last = ur.last_milestone()
    assert last is not None and last["kind"] == "reassign"


def test_interrupt_and_note_messages(ws_tmp):
    bb, ur = make_role(ws_tmp)
    ur.interrupt("task-9", note="先停下")
    assert ur.milestones()[-1]["kind"] == MilestoneKind.INTERRUPT.value
    # 插话不产生里程碑，仅高优广播
    ur.post_note("先读 tests 再动手", receiver="coder")
    assert len(ur.milestones()) == 1
    msgs = bb.user_messages()
    assert msgs[-1].type == MsgType.USER_MESSAGE.value
    assert msgs[-1].receiver == "coder"
    assert msgs[-1].payload["text"] == "先读 tests 再动手"


# ---- 黑板用户通道（2.3A） ----

def test_post_user_stamps_sender_priority_receiver():
    bb = Blackboard()
    msg = Message(sender="someone", receiver="", type="x")
    bb.post_user(msg)
    stored = bb.user_messages()[0]
    assert stored.sender == USER_SENDER
    assert stored.receiver == "*"
    assert stored.priority == USER_PRIORITY


def test_pending_user_messages_per_consumer_cursor(ws_tmp):
    _, ur = make_role(ws_tmp)
    ur.post_note("广播给所有人")
    ur.post_note("只给 coder", receiver="coder")
    ur.post_note("只给 reviewer", receiver="reviewer")
    bb = ur.blackboard
    # coder：应收到广播 + 定向 coder 两条
    coder_msgs = bb.pending_user_messages("coder")
    assert len(coder_msgs) == 2
    assert {m.receiver for m in coder_msgs} == {"*", "coder"}
    # 第二次读取为空（已消费）
    assert bb.pending_user_messages("coder") == []
    # reviewer 独立游标，仍能收到自己那条 + 广播（未被 coder 消费影响）
    rev_msgs = bb.pending_user_messages("reviewer")
    assert len(rev_msgs) == 2
    # 广播"*"消费者视角：全部三条
    assert len(bb.pending_user_messages("*")) == 3


def test_pending_user_messages_via_user_role(ws_tmp):
    _, ur = make_role(ws_tmp)
    ur.post_note("请检查命令注入", receiver="security")
    inbox = ur.pending_inputs("security")
    assert len(inbox) == 1
    assert inbox[0]["payload"]["text"] == "请检查命令注入"
    assert ur.pending_inputs("security") == []


def test_blackboard_summary_counts_user_messages(ws_tmp):
    bb, ur = make_role(ws_tmp)
    ur.post_note("x")
    ur.veto("t", "timing")
    assert bb.summary()["user_messages"] == 2
    assert bb.summary()["messages"] == 2


# ---- Orchestrator 接线与里程碑（2.3C） ----

def test_orchestrator_user_role_lazy_shared_and_milestones(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    dl = DecisionLogger()
    orch = OrchestratorAgent(config=cfg, blackboard=bb, decision_logger=dl)
    assert orch.user_role is orch.user_role  # 懒创建单例
    assert orch.user_role.blackboard is bb
    m = orch.veto_task("task-1", "direction", note="需求理解有偏差")
    assert m.kind == "veto"
    assert len(orch.user_milestones()) == 1
    assert orch.user_role.veto_reason("task-1") == "direction"
    assert any(d.name == "user.milestone" for d in dl.decisions)
    assert len(bb.user_messages()) == 1


def test_orchestrator_retry_gate_honours_veto_and_approve(ws_tmp):
    orch = OrchestratorAgent()
    assert orch._retry_blocked_by_user_veto("root-1", "task-x") is False
    orch.veto_task("root-1", "risk")
    assert orch._retry_blocked_by_user_veto("root-1", "task-x") is True
    # 未否决的其他任务不受影响
    assert orch._retry_blocked_by_user_veto("root-2", "task-x") is False
    orch.approve_task("root-1")
    assert orch._retry_blocked_by_user_veto("root-1", "task-x") is False


@pytest.mark.asyncio
async def test_full_run_result_default_user_milestones_empty(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    dl = DecisionLogger()
    plan = plan_llm_for({"instruction": "实现模块", "role": "coder",
                         "dependencies": [], "priority": 1})
    orch = make_orchestrator(
        cfg, bb,
        make_workers(cfg, bb,
                     ScriptedWorkerLLM('{"final_answer": "完成"}'),
                     ScriptedWorkerLLM(PASS_VERDICT)),
        plan, dl=dl,
    )
    result = await orch.run("实现模块")
    assert result.ok
    assert result.user_milestones == []
    # 运行后可对某个子任务下达用户决策并留痕
    task_id = result.subtasks[0]["id"]
    orch.veto_task(task_id, "timing")
    assert orch.user_milestones()
    assert orch.user_role.is_vetoed(task_id)


# ---- 会话档案里程碑过滤器（2.3C） ----

def test_session_replay_milestones_filters_user_decisions():
    archive = {
        "events": [{"ts": 0.5, "type": "think"}],
        "spans": [{"start_time": 0.8, "kind": "span", "name": "x"}],
        "decisions": [
            {"timestamp": 1.0, "name": "user.milestone"},
            {"timestamp": 2.0, "name": "user.milestone"},
            {"timestamp": 3.0, "name": "compression_level"},
        ],
    }
    replay = SessionReplay(archive)
    assert len(replay.timeline()) == 5
    milestones = replay.milestones()
    assert len(milestones) == 2
    assert all(row["kind"] == "decision" for row in milestones)
    assert all(str(row["payload"]["name"]).startswith("user.")
               for row in milestones)


def test_session_replay_milestones_empty_archive():
    replay = SessionReplay({"events": [], "spans": [], "decisions": []})
    assert replay.milestones() == []
