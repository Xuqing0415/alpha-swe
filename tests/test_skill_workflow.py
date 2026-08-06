"""阶段三测试：技能工作流（YAML 加载/匹配/展开 DAG/热加载/端到端执行/失败回退）。"""
import asyncio
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig, SkillConfig)
from agent.context.skill import SkillLibrary
from agent.core.decision_logger import DecisionLogger
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM

REST_SKILL = """name: add-rest-endpoint
version: "1.0.0"
description: 添加 REST 端点
priority: 8
triggers:
  keywords: [rest, api, 端点, endpoint]
steps:
  - name: route
    instruction: 定义 REST 端点路由
    dependencies: []
  - name: validation
    instruction: 编写请求参数校验
    dependencies: [route]
    on_failure: fallback
    fallback: 改用最简参数校验并重试
  - name: controller
    instruction: 实现端点控制器逻辑
    dependencies: [validation]
  - name: tests
    instruction: 编写测试用例
    dependencies: [controller]
"""


def write_skill(d: Path, name: str, body: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_yaml_skill_load_and_match(ws_tmp):
    d = ws_tmp / "skills"
    write_skill(d, "add-rest-endpoint", REST_SKILL)
    lib = SkillLibrary(skills_dir=str(d))
    skills = lib.list_skills()
    assert [s.name for s in skills] == ["add-rest-endpoint"]
    assert len(skills[0].steps) == 4

    matched = lib.match("为订单添加 REST API 端点")
    assert [s.name for s in matched] == ["add-rest-endpoint"]
    assert lib.match("无关的普通任务") == []


def test_expand_builds_dag_with_decision_points(ws_tmp):
    d = ws_tmp / "skills"
    write_skill(d, "add-rest-endpoint", REST_SKILL)
    lib = SkillLibrary(skills_dir=str(d))
    skill = lib.get("add-rest-endpoint")
    tasks = lib.expand(skill, "为订单添加 REST API 端点")
    by_name = {t.metadata["skill_step"]: t for t in tasks}
    assert set(by_name) == {"route", "validation", "controller", "tests"}
    # 依赖链：validation -> route -> (无)
    assert by_name["validation"].dependencies == [by_name["route"].id]
    assert by_name["controller"].dependencies == [by_name["validation"].id]
    assert by_name["tests"].dependencies == [by_name["controller"].id]
    assert by_name["route"].dependencies == []
    # 决策点写入 metadata
    assert by_name["validation"].metadata["on_failure"] == "fallback"
    assert by_name["validation"].metadata["fallback"] == "改用最简参数校验并重试"
    assert by_name["route"].metadata["on_failure"] == "abort"
    # 指令携带技能与原始任务上下文
    assert "技能 add-rest-endpoint" in by_name["route"].instruction
    assert "为订单添加 REST API 端点" in by_name["route"].instruction


def test_skill_hot_reload(ws_tmp):
    d = ws_tmp / "skills"
    d.mkdir(parents=True, exist_ok=True)
    lib = SkillLibrary(skills_dir=str(d))
    assert lib.list_skills() == []
    write_skill(d, "demo", """name: demo
triggers:
  keywords: [demo]
steps:
  - name: s1
    instruction: 第一步
""")
    assert [s.name for s in lib.match("跑一下 demo")] == ["demo"]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp: Path, skills_dir: Path):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"),
                            auto_experience=False),  # 测试不额外消耗 LLM 摘要
        mcp=MCPOptions(enabled=False),
        skills=SkillConfig(enabled=True, dir=str(skills_dir),
                           workflow_enabled=True, max_active=3, allow_fallback=True),
        plugin=__import__("agent.config", fromlist=["PluginConfig"]).PluginConfig(
            enabled=False, dir=str(ws_tmp / "plugins")),
    )


@pytest.mark.asyncio
async def test_end_to_end_skill_workflow_executes_in_order(ws_tmp):
    skills_dir = ws_tmp / "skills"
    write_skill(skills_dir, "add-rest-endpoint", REST_SKILL)
    # 每个步骤任务一个 final_answer（额外摘要调用会被捕获并回退规则提取）
    llm = ScriptedLLM(
        '{"final_answer": "路由完成"}',
        '{"final_answer": "校验完成"}',
        '{"final_answer": "控制器完成"}',
        '{"final_answer": "测试完成"}',
    )
    loop = AgentLoop(config=make_config(ws_tmp, skills_dir), llm=llm)
    result = await loop.run("为订单模块添加一个 REST API 端点")
    assert result.ok

    dag = loop.scheduler.dag
    skill_tasks = [t for t in dag.all() if t.metadata.get("skill") == "add-rest-endpoint"]
    assert len(skill_tasks) == 4
    order = [e["data"]["task_id"] for e in loop.events if e["type"] == "task_start"]
    ids = [t.id for t in skill_tasks]
    assert order == [t.id for t in skill_tasks]  # 依赖链保证按 route->validation->controller->tests 执行
    assert set(ids) == {
        "add-rest-endpoint::route", "add-rest-endpoint::validation",
        "add-rest-endpoint::controller", "add-rest-endpoint::tests",
    }
    # 决策日志包含技能激活与展开
    assert any(dp.name == "skill.activate" for dp in loop._decision.decisions)
    assert any(dp.name == "skill.expand" for dp in loop._decision.decisions)


@pytest.mark.asyncio
async def test_skill_step_failure_spawns_fallback(ws_tmp):
    skills_dir = ws_tmp / "skills"
    write_skill(skills_dir, "demo-fallback", """name: demo-fallback
triggers:
  keywords: [risky]
steps:
  - name: risky
    instruction: 执行有风险的操作
    on_failure: fallback
    fallback: 使用安全方案重试
""")
    # risky 步骤：两次无法解析的输出 -> 失败 -> 触发 fallback
    llm = ScriptedLLM('{"oops": 1}', '{"oops": 2}', '{"final_answer": "安全方案完成"}')
    loop = AgentLoop(config=make_config(ws_tmp, skills_dir), llm=llm)
    result = await loop.run("执行 risky 任务")
    assert not result.ok  # 原始步骤失败
    dag = loop.scheduler.dag
    failed = dag.get("demo-fallback::risky")
    assert failed is not None and failed.status.value == "failed"
    # fallback 任务被生成并执行完成
    fallback_tasks = [
        t for t in dag.all()
        if t.instruction.startswith("使用安全方案重试")
    ]
    assert fallback_tasks, "应生成 fallback 任务"
    assert fallback_tasks[0].status.value == "completed"
    # 决策日志记录 skill.step_fallback
    found = [
        dp for dp in loop._decision.decisions
        if dp.name == "skill.step_fallback"
    ]
    assert found, "决策日志缺少 skill.step_fallback"


@pytest.mark.asyncio
async def test_skill_step_orchestrate_emits_intervention(ws_tmp):
    skills_dir = ws_tmp / "skills"
    write_skill(skills_dir, "demo-orch", """name: demo-orch
triggers:
  keywords: [升级]
steps:
  - name: step1
    instruction: 需要审查的步骤
    on_failure: orchestrate
""")
    llm = ScriptedLLM('{"oops": 1}', '{"oops": 2}')
    loop = AgentLoop(config=make_config(ws_tmp, skills_dir), llm=llm)
    result = await loop.run("执行需要升级介入的任务")
    assert not result.ok
    assert any(dp.name == "skill.step_intervention" for dp in loop._decision.decisions)
    assert any(e["type"] == "skill_intervention" for e in loop.events)