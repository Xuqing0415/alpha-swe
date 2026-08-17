# -*- coding: utf-8 -*-
"""主线二 2.1：动态角色分配——角色库、角色需求规划、懒创建 Worker、跨角色黑板通信。"""
import json
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig, WorkerRoleConfig, load_team_config)
from agent.core.decision_logger import DecisionLogger
from agent.core.task import Task, TaskDAG
from agent.llm import MockLLM
from agent.multiagent import (Blackboard, Message, MsgType, OrchestratorAgent,
                              TeamPlanner, WorkerAgent)


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


# ---- 角色库 ----

def test_roles_from_config_includes_full_library():
    roles = TeamPlanner.roles_from_config()
    for name in ("coder", "reviewer", "tester", "debugger",
                 "architect", "documenter", "ops", "security"):
        assert name in roles, f"角色库缺少 {name}"


def test_team_config_loads_routing_keywords():
    roles = {r.name: r for r in load_team_config().roles}
    assert roles["debugger"].routing_keywords
    assert roles["documenter"].routing_keywords
    assert roles["security"].routing_keywords


# ---- 角色需求规划 ----

def test_team_planner_parses_new_role_with_rationale():
    planner = TeamPlanner(llm=MockLLM(), roles=TeamPlanner.roles_from_config())
    raw = ('[{"instruction": "更新 README 使用说明", "role": "documenter",'
           ' "role_rationale": "需要文档撰写能力", "dependencies": []}]')
    tasks = planner._parse(raw)
    assert len(tasks) == 1
    assert tasks[0].role == "documenter"
    assert tasks[0].metadata.get("role_rationale") == "需要文档撰写能力"


@pytest.mark.asyncio
async def test_team_planner_prompt_includes_role_descriptions():
    llm = MockLLM(responder=lambda msgs: json.dumps(
        [{"instruction": "更新 README", "role": "documenter"}],
        ensure_ascii=False))
    planner = TeamPlanner(
        llm=llm, roles=["coder", "documenter"],
        role_descriptions={"coder": "实现", "documenter": "负责文档与 README"},
    )
    tasks = await planner.plan("写文档")
    assert tasks[0].role == "documenter"
    user_content = llm.calls[0][1]["content"]
    assert "负责文档与 README" in user_content, "规划 Prompt 应包含角色职责说明"


# ---- 关键词路由（动态角色回退） ----

def test_classify_role_dynamic_library():
    assert TeamPlanner._classify_role("审查代码规范") == "reviewer"
    assert TeamPlanner._classify_role("为模块编写测试") == "tester"
    assert TeamPlanner._classify_role("实现一个缓存函数") == "coder"
    assert TeamPlanner._classify_role("定位崩溃根因并修复") == "debugger"
    assert TeamPlanner._classify_role("更新 README 文档") == "documenter"
    assert TeamPlanner._classify_role("设计 API 架构方案") == "architect"
    assert TeamPlanner._classify_role("部署到 CI 环境") == "ops"
    assert TeamPlanner._classify_role("修复 SQL 注入漏洞") == "security"


def test_classify_role_tester_beats_coder_keyword():
    # "编写"是 coder 关键词，但 tester 优先级更高
    assert TeamPlanner._classify_role("编写测试用例并运行") == "tester"


# ---- 懒创建未预配置角色 ----

@pytest.mark.asyncio
async def test_orchestrator_lazy_instantiates_unconfigured_role(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    dl = DecisionLogger()

    class DocPlanner:
        async def plan(self, prompt):
            return [Task(id="s0", instruction="更新 README 使用说明",
                         role="documenter")]

    doc_llm = ScriptedWorkerLLM('{"final_answer": "README 已更新"}')
    orch = OrchestratorAgent(
        config=cfg, blackboard=bb, decision_logger=dl,
        llm=doc_llm, workers={}, planner=DocPlanner(), concurrency=1,
    )
    assert "documenter" not in orch.workers
    result = await orch.run("更新 README")
    assert result.ok
    assert "documenter" in orch.workers, "documenter 应被懒创建"
    assert "README 已更新" in result.final_answer
    routed = [d for d in dl.decisions if d.name == "role.routing"]
    assert routed and "自动实例化" in routed[0].decision, \
        "决策日志应记录懒创建"


# ---- 跨角色黑板通信 ----

def test_upstream_artifacts_flow_between_roles(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    bb.publish("task:s0", {
        "files": {"README.md": "# 项目说明"}, "output": "产出完成", "ok": True,
    })
    orch = OrchestratorAgent(config=cfg, blackboard=bb, workers={})
    orch._dag = TaskDAG()
    s0 = Task(id="s0", instruction="撰写文档", role="documenter")
    s1 = Task(id="s1", instruction="基于上游整理", role="documenter",
              dependencies=["s0"])
    orch._dag.add(s0)
    orch._dag.add(s1)
    text = orch._upstream_artifacts(s1)
    assert "上游任务 s0" in text
    assert "README.md" in text
    assert "项目说明" in text


def test_lazy_worker_reuses_orchestrator_llm(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    roles = {r.name: r for r in load_team_config().roles}
    orch = OrchestratorAgent(
        config=cfg, blackboard=bb,
        roles_config=[roles["documenter"]],
        workers={}, llm=ScriptedWorkerLLM('{"final_answer": "ok"}'),
    )
    assert "documenter" in orch._role_map
    assert orch._role_map["documenter"].tools == ["file_ops", "file_search"]
