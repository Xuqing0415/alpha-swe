# -*- coding: utf-8 -*-
"""交叉集成：项目记忆 x 多 Agent 共享、能力画像 x 角色分配。"""
import json
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig, WorkerRoleConfig)
from agent.core.decision_logger import DecisionLogger
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.memory.shared import SharedMemoryStore, _lock_for
from agent.memory.store import SqliteMemoryStore
from agent.multiagent import Blackboard, OrchestratorAgent, TeamPlanner, WorkerAgent
from agent.selfimprove.capability import CapabilityProfile


def make_config(ws_tmp: Path, improve_dir: str = "improve") -> AppConfig:
    return AppConfig(
        agent=AgentConfig(
            max_rounds=8, max_retries=2, max_concurrency=1,
            self_improve_dir=str(ws_tmp / improve_dir),
        ),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(backend="sqlite", db_path=str(ws_tmp / "mem.db")),
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


# ============ 交叉集成一：项目记忆 x 多 Agent 共享 ============

def test_shared_memory_tags_creator(ws_tmp):
    inner = SqliteMemoryStore(db_path=str(ws_tmp / "m1.db"))
    shared = SharedMemoryStore(inner, creator="coder",
                               lock_key=str(ws_tmp / "m1.db"))
    shared.remember("note", "AuthService 有 N+1 查询问题", {"level": "high"})
    hits = inner.search("查询问题", kinds=["note"])
    assert hits
    meta = hits[0].get("metadata") or {}
    assert meta.get("creator") == "coder", "写记忆应自动标记创建者"
    shared.close()


def test_shared_memory_same_backend_shared_lock(ws_tmp):
    key = str(ws_tmp / "shared.db")
    a = SharedMemoryStore(SqliteMemoryStore(db_path=key), creator="a", lock_key=key)
    b = SharedMemoryStore(SqliteMemoryStore(db_path=key), creator="b", lock_key=key)
    assert a._lock is b._lock, "同一后端应共享写锁（写入串行化）"
    assert _lock_for(key) is a._lock
    a.close(); b.close()


def test_shared_memory_private_only_visible_to_creator(ws_tmp):
    key = str(ws_tmp / "priv.db")
    inner = SqliteMemoryStore(db_path=key)
    alice = SharedMemoryStore(inner, creator="alice", lock_key=key)
    bob = SharedMemoryStore(inner, creator="bob", lock_key=key)
    alice.remember("note", "alice 的私有尝试记录", {"private": True})
    alice.remember("note", "alice 的公开经验", {})
    assert alice.search("私有尝试") , "创建者可看到自己的私有记忆"
    assert not bob.search("私有尝试"), "其他 Agent 看不到私有记忆"
    assert bob.search("公开经验"), "公开记忆所有 Agent 可见"
    alice.close(); bob.close()


@pytest.mark.asyncio
async def test_agent_loop_memory_creator_wires_shared_store(ws_tmp):
    cfg = make_config(ws_tmp)
    loop = AgentLoop(config=cfg, llm=MockLLM(), memory_creator="reviewer")
    try:
        assert isinstance(loop.memory, SharedMemoryStore)
        assert loop.memory.creator == "reviewer"
        loop.memory.remember("note", "评审发现边界问题")
        hits = loop.memory.search("边界")
        assert hits
        assert (hits[0].get("metadata") or {}).get("creator") == "reviewer"
    finally:
        await loop.close()


# ============ 交叉集成二：能力画像 x 角色分配 ============

def test_capability_role_identity_persists_across_instances(ws_tmp):
    base = ws_tmp / "improve"
    p1 = CapabilityProfile.for_role("debugger", base_dir=str(base))
    assert p1.path == base / "capability" / "debugger.json"
    p1.record("定位登录空指针崩溃并修复", True)
    p1.close()
    # 新实例（模拟下一次会话）读取同一角色画像
    p2 = CapabilityProfile.for_role("debugger", base_dir=str(base))
    assert p2.score("debug") > 0, "角色画像应跨实例持久化"
    assert p2.identity == "debugger"
    p2.close()


def test_capability_role_hint_text(ws_tmp):
    base = ws_tmp / "improve"
    p = CapabilityProfile.for_role("tester", base_dir=str(base))
    p.record("为登录模块编写测试", True)
    p.record("为缓存模块编写测试", True)
    hint = p.role_hint_text()
    assert hint, "有数据时应生成角色画像提示"
    assert "测试编写" in hint
    p.close()


def test_classify_role_capability_tiebreak():
    # 无能力分：按优先级 security > coder
    assert TeamPlanner._classify_role("编写安全补丁") == "security"
    # 有能力分：coder 的代码修改分更高 -> 路由到 coder
    scores = {"coder": 0.9, "security": 0.2}
    assert TeamPlanner._classify_role(
        "编写安全补丁", capability_scores=scores) == "coder"
    # 能力分都为零时不改变默认优先级
    assert TeamPlanner._classify_role(
        "编写安全补丁", capability_scores={}) == "security"


@pytest.mark.asyncio
async def test_team_planner_injects_capability_hint(ws_tmp):
    base = ws_tmp / "improve"
    prof = CapabilityProfile.for_role("documenter", base_dir=str(base))
    prof.record("更新 README 文档", True)
    llm = MockLLM(responder=lambda msgs: json.dumps(
        [{"instruction": "更新 README", "role": "documenter"}],
        ensure_ascii=False))
    planner = TeamPlanner(
        llm=llm, roles=["coder", "documenter"],
        capability_profiles={"documenter": prof},
    )
    tasks = await planner.plan("写文档")
    assert tasks[0].role == "documenter"
    user_content = llm.calls[0][1]["content"]
    assert "文档编写" in user_content, "规划 Prompt 应注入角色能力画像"
    prof.close()


@pytest.mark.asyncio
async def test_worker_records_outcome_into_role_profile(ws_tmp):
    cfg = make_config(ws_tmp)
    role = WorkerRoleConfig(name="coder", tools=["terminal_execute", "file_ops"])
    worker = WorkerAgent(role, config=cfg, blackboard=Blackboard())
    llm = ScriptedWorkerLLM('{"final_answer": "实现完成"}')
    worker.llm = llm
    result = await worker.execute_task(
        Task(id="t1", instruction="实现缓存函数", role="coder"))
    assert result.ok
    assert worker.capability is not None
    assert worker.capability.score("code_modify") > 0, \
        "Worker 完成任务后应更新角色能力画像"
    # 画像已落盘（角色身份持久化）
    path = ws_tmp / "improve" / "capability" / "coder.json"
    assert path.exists()


@pytest.mark.asyncio
async def test_orchestrator_wires_role_profiles_and_capability_routing(ws_tmp):
    cfg = make_config(ws_tmp)
    roles = [
        WorkerRoleConfig(name="coder",
                         tools=["terminal_execute", "file_ops"]),
        WorkerRoleConfig(name="security",
                         tools=["terminal_execute", "file_ops"],
                         routing_keywords=["安全", "补丁", "漏洞"]),
        WorkerRoleConfig(name="reviewer", read_only=True,
                         tools=["file_ops"],
                         routing_keywords=["审查"]),
    ]
    dl = DecisionLogger()
    orch = OrchestratorAgent(
        config=cfg, decision_logger=dl, roles_config=roles,
        llm=MockLLM(), workers={}, concurrency=1,
    )
    assert set(orch._role_profiles) >= {"coder", "security", "reviewer"}
    assert orch.planner.capability_profiles is orch._role_profiles

    # 预置画像：coder 的代码修改能力强
    orch._role_profiles["coder"].record("实现缓存模块", True)
    orch._role_profiles["coder"].record("重构数据处理", True)
    orch._role_profiles["security"].record("修复注入漏洞", False)
    for p in orch._role_profiles.values():
        p.close()

    # 指令同时命中 coder(编写) 与 security(安全)：能力分高者胜出
    role = TeamPlanner._classify_role(
        "编写安全补丁",
        capability_scores={name: p.score_for_instruction("编写安全补丁")
                           for name, p in orch._role_profiles.items()},
    )
    assert role == "coder", "多角色命中时应优先高能力分角色"
