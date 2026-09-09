"""主线二 2.1B/2.2 测试：角色权限预检 + 方案辩论深化。

覆盖：预检只读角色改派 / reviewer 放行 / 无工具角色改派 / coder 缺失升级；
辩论分歧锚定、Critic 确定性评估、NEEDS_USER 升级、2 轮上限、回溯验证与
决策日志；Orchestrator 的 open/critic/verify/summary 接线。全部离线确定性。
"""
import json
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig, WorkerRoleConfig)
from agent.core.decision_logger import DecisionLogger
from agent.core.task import Task, TaskStatus
from agent.llm import MockLLM
from agent.multiagent import (Blackboard, CriticVerdict, DebateCoordinator,
                              DebateOption, OrchestratorAgent, WorkerAgent)
from agent.multiagent.debate import (MAX_ROUNDS, STATUS_NEEDS_USER,
                                     STATUS_OPEN, STATUS_RESOLVED)


def make_config(ws_tmp: Path) -> AppConfig:
    return AppConfig(
        agent=AgentConfig(max_rounds=8, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(backend="hybrid", db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


class ScriptedWorkerLLM(MockLLM):
    """Worker 用 LLM：经验总结器返回空对象，其余按脚本顺序消费。"""

    def __init__(self, *responses: str):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        system = messages[0].get("content", "") if messages else ""
        if "经验总结器" in system:
            return "{}"
        assert self._responses, "Worker LLM 脚本响应已耗尽"
        return self._responses.pop(0)


def two_options():
    return [
        DebateOption(id="A", label="方案A", statement="直接重构",
                     pros=["结构清晰"], cons=["改动大"], risks=["回归风险"]),
        DebateOption(id="B", label="方案B", statement="增量调整",
                     pros=["风险低"], cons=["周期长"], risks=["遗留债务"]),
    ]


def coder_role():
    return WorkerRoleConfig(name="coder", tools=["file_ops", "terminal_execute"])


# ---- 2.2A 分歧锚定 ----

def test_open_debate_requires_anchor_and_min_two_options():
    coordinator = DebateCoordinator()
    with pytest.raises(ValueError):
        coordinator.open_debate("", two_options())          # anchor 缺失
    with pytest.raises(ValueError):
        coordinator.open_debate("   ", two_options())       # anchor 全空白
    with pytest.raises(ValueError):
        coordinator.open_debate("分歧", [DebateOption(id="A")])  # 少于 2 个


def test_open_debate_records_opening_statements_and_full_schema():
    coordinator = DebateCoordinator()
    session = coordinator.open_debate(
        "选哪个改造方案？", two_options(), question="直接重写还是渐进调整")
    assert session.status == STATUS_OPEN
    assert session.rounds == 0
    data = session.to_dict()
    assert data["session_id"] == session.session_id
    assert data["anchor"] == "选哪个改造方案？"
    assert data["question"] == "直接重写还是渐进调整"
    assert len(data["transcript"]) == 2
    for entry in data["transcript"]:
        assert set(entry) == {"round", "option_id", "speaker", "text"}
        assert entry["round"] == 1
    statements = {entry["option_id"]: entry["text"]
                  for entry in data["transcript"]}
    assert statements == {"A": "直接重构", "B": "增量调整"}
    assert data["options"][0]["pros"] == ["结构清晰"]
    assert data["resolution"] is None
    assert data["verification"] == []


def test_critic_deterministic_recommends_highest_total_with_schema():
    coordinator = DebateCoordinator()
    session = coordinator.open_debate("选哪个改造方案？", two_options())
    verdict = coordinator.critic_evaluate(session.session_id, criteria_scores={
        "A": {"quality": 5, "speed": 5},
        "B": {"quality": 4, "speed": 0},
    })
    assert isinstance(verdict, CriticVerdict)
    assert verdict.recommendation == "A"
    assert verdict.confidence == pytest.approx(0.6)   # (10-4)/10
    assert verdict.method == "deterministic"
    assert session.status == STATUS_RESOLVED
    assert session.rounds == 1
    assert session.resolution["option_id"] == "A"
    assert session.resolution["confidence"] == pytest.approx(0.6)
    critic = session.to_dict()["critics"][0]
    for key in ("recommendation", "confidence", "criteria_scores",
                "key_reasons", "unresolved_concerns", "method", "round"):
        assert key in critic
    assert critic["criteria_scores"]["A"] == {"quality": 5, "speed": 5}
    # 已决议会话不再消耗新轮次
    again = coordinator.critic_evaluate(session.session_id, criteria_scores={
        "A": {"quality": 1}, "B": {"quality": 9},
    })
    assert session.rounds == 1
    assert again.recommendation == "A"


def test_critic_tie_or_no_scores_upgrade_needs_user():
    coordinator = DebateCoordinator()
    session = coordinator.open_debate("选哪个改造方案？", two_options())
    verdict = coordinator.critic_evaluate(session.session_id, criteria_scores={
        "A": {"quality": 3}, "B": {"quality": 3},
    })
    assert verdict.recommendation == ""
    assert verdict.confidence == 0.0
    assert session.status == STATUS_NEEDS_USER
    assert session.resolution is None

    another = coordinator.open_debate("另一分歧", two_options())
    coordinator.critic_evaluate(another.session_id, criteria_scores={
        "A": {}, "B": {},
    })
    assert another.status == STATUS_NEEDS_USER  # 全部 0 分 = 无法判定


def test_critic_low_confidence_upgrades_and_override_resolves():
    coordinator = DebateCoordinator()
    session = coordinator.open_debate("选哪个改造方案？", two_options())
    verdict = coordinator.critic_evaluate(session.session_id, criteria_scores={
        "A": {"quality": 20}, "B": {"quality": 18},
    })
    assert verdict.recommendation == "A"
    assert verdict.confidence == pytest.approx(0.1)   # 低于阈值 0.15
    assert session.status == STATUS_NEEDS_USER        # 不强制拍板
    assert session.resolution is None

    another = coordinator.open_debate("另一分歧", two_options())
    override = coordinator.critic_evaluate(
        another.session_id,
        criteria_scores={"A": {"quality": 20}, "B": {"quality": 18}},
        confidence=0.4,
    )
    assert override.confidence == pytest.approx(0.4)
    assert another.status == STATUS_RESOLVED


def test_round_cap_auto_upgrades_needs_user_to_stop_debate():
    responses = iter([
        json.dumps({"status": "open", "key_reasons": ["论据不足，再辩一轮"]}),
        json.dumps({"status": "open", "key_reasons": ["仍无法收敛"]}),
    ])
    coordinator = DebateCoordinator(
        llm=lambda session: next(responses))
    session = coordinator.open_debate("选哪个改造方案？", two_options())
    coordinator.critic_evaluate(session.session_id)
    assert session.status == STATUS_OPEN
    assert session.rounds == 1
    coordinator.critic_evaluate(session.session_id)
    assert session.rounds == MAX_ROUNDS
    assert session.status == STATUS_NEEDS_USER   # 上限仍未决议 -> 自动升级
    # 已锁定：再评估不再消耗轮次（防无限辩论）
    coordinator.critic_evaluate(session.session_id)
    assert session.rounds == MAX_ROUNDS
    assert len(session.critics) == MAX_ROUNDS


def test_llm_path_resolves_and_failures_fallback_without_raise():
    coordinator = DebateCoordinator(llm=lambda session: json.dumps({
        "recommendation": "B",
        "confidence": 0.8,
        "criteria_scores": {"A": {"x": 1}, "B": {"x": 2}},
        "key_reasons": ["B 更符合约束"],
        "unresolved_concerns": ["成本略高"],
    }))
    session = coordinator.open_debate("选哪个改造方案？", two_options())
    verdict = coordinator.critic_evaluate(session.session_id)
    assert verdict.recommendation == "B"
    assert verdict.method == "llm"
    assert session.status == STATUS_RESOLVED

    def broken(session):
        raise RuntimeError("llm 崩溃")

    broken_coordinator = DebateCoordinator(llm=broken)
    s1 = broken_coordinator.open_debate("另一分歧", two_options())
    v1 = broken_coordinator.critic_evaluate(s1.session_id)   # 绝不抛异常
    assert v1.recommendation == ""
    assert s1.status == STATUS_NEEDS_USER

    bad_json = DebateCoordinator(llm=lambda session: "{not json")
    s2 = bad_json.open_debate("另一分歧", two_options())
    v2 = bad_json.critic_evaluate(s2.session_id)
    assert v2.recommendation == ""
    assert s2.status == STATUS_NEEDS_USER

    invalid_option = DebateCoordinator(llm=lambda session: json.dumps({
        "recommendation": "Z", "confidence": 0.9,
    }))
    s3 = invalid_option.open_debate("另一分歧", two_options())
    v3 = invalid_option.critic_evaluate(s3.session_id)
    assert v3.recommendation == ""      # 方案不在会话中 -> 无法判定
    assert s3.status == STATUS_NEEDS_USER


# ---- 2.2C 回溯验证 ----

def test_record_verification_good_and_misjudged_with_summary():
    coordinator = DebateCoordinator()
    good_session = coordinator.open_debate("分歧A", two_options())
    coordinator.critic_evaluate(good_session.session_id, criteria_scores={
        "A": {"x": 10}, "B": {"x": 2},
    })
    entry = coordinator.record_verification(good_session.session_id, True)
    assert entry["outcome"] == "verified_good"
    assert good_session.critic_misses == 0

    bad_session = coordinator.open_debate("分歧B", two_options())
    coordinator.critic_evaluate(bad_session.session_id, criteria_scores={
        "A": {"x": 8}, "B": {"x": 3},
    })
    entry = coordinator.record_verification(bad_session.session_id, False)
    assert entry["outcome"] == "misjudged"
    assert bad_session.critic_misses == 1
    assert coordinator.verification_summary() == {
        "total": 2, "good": 1, "misjudged": 1}


def test_effective_confidence_drops_with_critic_misses():
    coordinator = DebateCoordinator()
    session = coordinator.open_debate("分歧", two_options())
    coordinator.critic_evaluate(session.session_id, criteria_scores={
        "A": {"x": 10}, "B": {"x": 4},
    })
    assert coordinator.effective_confidence(session.session_id) == \
        pytest.approx(0.6)
    coordinator.record_verification(session.session_id, False)
    assert coordinator.effective_confidence(session) == pytest.approx(0.54)
    coordinator.record_verification(session.session_id, False)
    assert coordinator.effective_confidence(session.session_id) == \
        pytest.approx(0.48)
    for _ in range(3):
        coordinator.record_verification(session.session_id, False)
    # 5 次失误 -> 惩罚封顶 0.5：0.6 × (1 - 0.5)
    assert session.critic_misses == 5
    assert coordinator.effective_confidence(session.session_id) == \
        pytest.approx(0.3)
    assert session.to_dict()["effective_confidence"] == pytest.approx(0.3)


def test_decision_logger_records_debate_key_points():
    logger = DecisionLogger()
    coordinator = DebateCoordinator(decision_logger=logger)
    session = coordinator.open_debate("选哪个改造方案？", two_options())
    coordinator.critic_evaluate(session.session_id, criteria_scores={
        "A": {"x": 5}, "B": {"x": 5},
    })
    resolved = coordinator.open_debate("另一分歧", two_options())
    coordinator.critic_evaluate(resolved.session_id, criteria_scores={
        "A": {"x": 10}, "B": {"x": 4},
    })
    coordinator.record_verification(resolved.session_id, True)
    names = [record.name for record in logger.decisions]
    assert "debate.open" in names
    assert "debate.critic" in names
    assert "debate.needs_user" in names
    assert "debate.resolved" in names
    assert "debate.verify" in names
    open_record = next(r for r in logger.decisions if r.name == "debate.open")
    assert open_record.config_key == "team.debate"
    assert open_record.config_value == session.session_id
    assert "锚定分歧" in open_record.decision


# ---- 2.1B 角色权限预检 ----

def test_preflight_readonly_role_with_write_intent_reassigns_coder(ws_tmp):
    cfg = make_config(ws_tmp)
    logger = DecisionLogger()
    readonly = WorkerRoleConfig(
        name="archivist", tools=["file_ops", "file_search"], read_only=True)
    orchestrator = OrchestratorAgent(
        config=cfg, roles_config=[readonly, coder_role()],
        workers={}, decision_logger=logger)
    task = Task(id="s0", instruction="修改该配置并实现新功能", role="archivist")
    final_role = orchestrator._preflight_role(task, "archivist")
    assert final_role == "coder"
    assert task.status != TaskStatus.FAILED
    assert orchestrator.needs_intervention is False
    preflight = [r for r in logger.decisions if r.name == "role.preflight"]
    assert preflight and "改派 coder" in preflight[0].decision


def test_preflight_reviewer_allowed_on_write_intent(ws_tmp):
    cfg = make_config(ws_tmp)
    logger = DecisionLogger()
    reviewer = WorkerRoleConfig(
        name="reviewer", tools=["file_ops"], read_only=True)
    orchestrator = OrchestratorAgent(
        config=cfg, roles_config=[reviewer],
        workers={}, decision_logger=logger)
    task = Task(id="s0", instruction="审查模块并修改评审意见", role="reviewer")
    assert orchestrator._preflight_role(task, "reviewer") == "reviewer"
    assert task.status != TaskStatus.FAILED
    assert not any(r.name == "role.preflight" for r in logger.decisions)


def test_preflight_no_tool_role_reassigns_or_fails_without_coder(ws_tmp):
    cfg = make_config(ws_tmp)
    no_tool = WorkerRoleConfig(name="ghost", tools=[])
    with_coder = OrchestratorAgent(
        config=cfg, roles_config=[no_tool, coder_role()],
        workers={}, decision_logger=DecisionLogger())
    task = Task(id="s0", instruction="实现一个功能", role="ghost")
    assert with_coder._preflight_role(task, "ghost") == "coder"

    without_coder = OrchestratorAgent(
        config=cfg, roles_config=[no_tool], workers={},
        decision_logger=DecisionLogger())
    task2 = Task(id="s1", instruction="实现一个功能", role="ghost")
    assert without_coder._preflight_role(task2, "ghost") == "ghost"
    assert task2.status == TaskStatus.FAILED
    assert without_coder.needs_intervention is True
    assert "无可用工具" in (task2.error or "")


def test_preflight_readonly_role_without_coder_intervenes(ws_tmp):
    cfg = make_config(ws_tmp)
    readonly = WorkerRoleConfig(
        name="archivist", tools=["file_ops"], read_only=True)
    orchestrator = OrchestratorAgent(
        config=cfg, roles_config=[readonly], workers={},
        decision_logger=DecisionLogger())
    task = Task(id="s0", instruction="修改接口定义", role="archivist")
    assert orchestrator._preflight_role(task, "archivist") == "archivist"
    assert task.status == TaskStatus.FAILED
    assert orchestrator.needs_intervention is True
    assert "需人工介入" in (task.error or "")


def test_preflight_unknown_role_and_clean_role_pass_through(ws_tmp):
    cfg = make_config(ws_tmp)
    logger = DecisionLogger()
    orchestrator = OrchestratorAgent(
        config=cfg, roles_config=[coder_role()],
        workers={}, decision_logger=logger)
    # 角色不在角色库：回退链已处理，不重复预检
    ghost = Task(id="s0", instruction="实现一个功能", role="ghost")
    assert orchestrator._preflight_role(ghost, "ghost") == "ghost"
    # 写角色 + 写意图原样返回（无需预检介入）
    write_task = Task(id="s1", instruction="修改代码", role="coder")
    assert orchestrator._preflight_role(write_task, "coder") == "coder"
    assert not any(r.name == "role.preflight" for r in logger.decisions)


@pytest.mark.asyncio
async def test_dispatch_readonly_role_reassigned_to_coder(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    logger = DecisionLogger()
    from agent.config import load_team_config
    roles = {r.name: r for r in load_team_config().roles}
    coder_llm = ScriptedWorkerLLM('{"final_answer": "已由 coder 完成改造"}')

    class ArchitectPlanner:
        async def plan(self, prompt):
            return [Task(id="s0", instruction="修改架构并实现该模块",
                         role="architect")]

    orchestrator = OrchestratorAgent(
        config=cfg, blackboard=bb, decision_logger=logger,
        llm=coder_llm,
        workers={
            "architect": WorkerAgent(
                roles["architect"], config=cfg,
                llm=ScriptedWorkerLLM('{"final_answer": "不应执行"}'),
                blackboard=bb),
            "coder": WorkerAgent(
                roles["coder"], config=cfg, llm=coder_llm, blackboard=bb),
        },
        planner=ArchitectPlanner(), concurrency=1)
    result = await orchestrator.run("改造架构")
    assert result.ok
    assert "已由 coder 完成改造" in result.final_answer
    preflight = [r for r in logger.decisions if r.name == "role.preflight"]
    assert preflight and "改派 coder" in preflight[0].decision
    assert orchestrator.needs_intervention is False


@pytest.mark.asyncio
async def test_dispatch_no_tool_role_fails_without_coder(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    logger = DecisionLogger()

    class GhostPlanner:
        async def plan(self, prompt):
            return [Task(id="s0", instruction="实现一个功能", role="ghost")]

    no_tool = WorkerRoleConfig(name="ghost", tools=[])
    orchestrator = OrchestratorAgent(
        config=cfg, blackboard=bb, roles_config=[no_tool],
        workers={}, planner=GhostPlanner(), concurrency=1,
        decision_logger=logger)
    result = await orchestrator.run("实现一个功能")
    assert result.ok is False
    assert result.needs_intervention is True
    assert any("无可用工具" in (t.get("error") or "")
               for t in result.subtasks)


def test_orchestrator_debate_wiring_with_lazy_coordinator(ws_tmp):
    cfg = make_config(ws_tmp)
    logger = DecisionLogger()
    orchestrator = OrchestratorAgent(config=cfg, decision_logger=logger)
    assert orchestrator._debate_coordinator is None
    session = orchestrator.open_team_debate(
        "选哪个改造方案？", two_options(), question="内部 API 兼容性")
    assert session.status == STATUS_OPEN
    assert orchestrator._debate_coordinator is not None   # 首次使用懒创建
    verdict = orchestrator.critic_team_debate(
        session.session_id,
        criteria_scores={"A": {"x": 10}, "B": {"x": 4}})
    assert verdict.recommendation == "A"
    assert session.status == STATUS_RESOLVED
    orchestrator.verify_team_debate(session.session_id, True)
    summary = orchestrator.debate_summary()
    assert summary["sessions"] == 1
    assert summary["status"][STATUS_RESOLVED] == 1
    assert summary["verification"] == {"total": 1, "good": 1, "misjudged": 0}
    names = [record.name for record in logger.decisions]
    assert "debate.open" in names
    assert "debate.resolved" in names
    assert "debate.verify" in names


def test_orchestrator_uses_provided_debate_coordinator(ws_tmp):
    cfg = make_config(ws_tmp)
    shared = DebateCoordinator(decision_logger=DecisionLogger())
    orchestrator = OrchestratorAgent(
        config=cfg, debate_coordinator=shared, decision_logger=DecisionLogger())
    assert orchestrator._debate_coordinator is shared
    session = orchestrator.open_team_debate("选哪个改造方案？", two_options())
    assert session.session_id in shared._sessions
