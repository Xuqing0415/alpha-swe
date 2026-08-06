"""阶段二深化测试：角色只读权限、消息优先级/超时、自动路由回退、评审升级介入、缺陷注入闭环。"""
import json
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig, WorkerRoleConfig)
from agent.core.decision_logger import DecisionLogger
from agent.core.task import Task
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
    def __init__(self, *responses: str):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        system = messages[0].get("content", "") if messages else ""
        if "经验总结器" in system:
            return "{}"
        assert self._responses, "Worker LLM 脚本响应已耗尽"
        return self._responses.pop(0)


PASS_VERDICT = '{"final_answer": "{\\"verdict\\": \\"pass\\", \\"suggestion\\": \\"\\"}"}'
RETRY_VERDICT = ('{"final_answer": "{\\"verdict\\": \\"retry\\", '
                 '\\"suggestion\\": \\"使用环境变量存储密码，禁止硬编码\\"}"}')


def roles_from(extra=None):
    from agent.config import load_team_config
    roles = {r.name: r for r in load_team_config().roles}
    for k, v in (extra or {}).items():
        roles[k] = v
    return roles


def make_workers(cfg, bb, coder_llm, reviewer_llm, tester_llm=None):
    roles = roles_from()
    workers = {
        "coder": WorkerAgent(roles["coder"], config=cfg, llm=coder_llm, blackboard=bb),
        "reviewer": WorkerAgent(roles["reviewer"], config=cfg, llm=reviewer_llm, blackboard=bb),
    }
    if tester_llm is not None:
        workers["tester"] = WorkerAgent(roles["tester"], config=cfg, llm=tester_llm, blackboard=bb)
    return workers


def plan_llm_for(*items):
    return MockLLM(responder=lambda msgs: json.dumps(list(items), ensure_ascii=False))


def make_orchestrator(cfg, bb, workers, plan_llm, dl=None, max_review_retries=2):
    return OrchestratorAgent(
        config=cfg,
        blackboard=bb,
        workers=workers,
        planner=TeamPlanner(llm=plan_llm, roles=list(workers.keys())),
        max_review_retries=max_review_retries,
        concurrency=1,
        decision_logger=dl,
    )


# ---- 1. 角色只读权限 ----

def test_reviewer_role_builds_read_only_tools(ws_tmp):
    from agent.config import load_team_config
    reviewer_role = next(r for r in load_team_config().roles if r.name == "reviewer")
    assert reviewer_role.read_only is True, "team.yaml 中 reviewer 应标记只读"

    cfg = make_config(ws_tmp)
    worker = WorkerAgent(reviewer_role, config=cfg, llm=MockLLM())
    manager = worker._role_tools()
    file_tool = manager.get("file_ops")
    term_tool = manager.get("terminal_execute")
    assert file_tool.read_only is True
    assert term_tool.read_only is True
    # coder 不受影响
    coder_role = WorkerRoleConfig(name="coder", tools=["file_ops", "terminal_execute"])
    coder_worker = WorkerAgent(coder_role, config=cfg, llm=MockLLM())
    assert coder_worker._role_tools().get("file_ops").read_only is False


@pytest.mark.asyncio
async def test_read_only_tool_blocks_writes_and_commands(ws_tmp):
    from agent.tools.base import ExecutionContext
    from agent.config import load_team_config
    reviewer_role = next(r for r in load_team_config().roles if r.name == "reviewer")
    cfg = make_config(ws_tmp)
    worker = WorkerAgent(reviewer_role, config=cfg, llm=MockLLM())
    manager = worker._role_tools()
    ws = str(ws_tmp / "ws")
    ctx = ExecutionContext(workspace=ws)

    r = await manager.execute("file_ops", {"action": "write", "path": "a.py", "content": "x"}, ctx)
    assert not r.success and "只读" in (r.error or "")
    (ws_tmp / "ws").mkdir(parents=True, exist_ok=True)
    (ws_tmp / "ws" / "a.py").write_text("print(1)", encoding="utf-8")
    r2 = await manager.execute("file_ops", {"action": "read", "path": "a.py"}, ctx)
    assert r2.success

    r3 = await manager.execute("terminal_execute", {"command": "rm -rf x"}, ctx)
    assert not r3.success and "只读" in (r3.error or "")
    r4 = await manager.execute("terminal_execute", {"command": "cat a.py"}, ctx)
    assert r4.success
    r5 = await manager.execute("terminal_execute", {"command": "cat a.py > b.txt"}, ctx)
    assert not r5.success and "只读" in (r5.error or "")


# ---- 2. 消息优先级/超时 ----

def test_message_priority_and_timeout_fields():
    m = Message(sender="orchestrator", receiver="coder", type=MsgType.TASK_ASSIGN,
                payload={"task_id": "t1"}, priority=5, timeout=30.0)
    d = m.to_dict()
    assert d["priority"] == 5
    assert d["timeout"] == 30.0
    m2 = Message(sender="a", receiver="b", type=MsgType.TASK_RESULT)
    assert m2.to_dict()["priority"] == 0
    assert m2.to_dict()["timeout"] is None


@pytest.mark.asyncio
async def test_task_assign_message_carries_priority_and_timeout(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    plan_llm = plan_llm_for(
        {"instruction": "实现模块", "role": "coder", "dependencies": [], "priority": 3},
    )
    orch = make_orchestrator(
        cfg, bb, make_workers(cfg, bb,
                              ScriptedWorkerLLM('{"final_answer": "完成"}'),
                              ScriptedWorkerLLM(PASS_VERDICT)),
        plan_llm,
    )
    await orch.run("实现模块")
    assign = next(m for m in bb.messages() if m.type == MsgType.TASK_ASSIGN.value)
    assert assign.priority == 3
    assert assign.timeout == cfg.team.message_timeout


# ---- 3. 自动路由 ----

def test_team_planner_classifies_role_by_instruction():
    assert TeamPlanner._classify_role("审查代码规范与安全隐患") == "reviewer"
    assert TeamPlanner._classify_role("为模块编写测试") == "tester"
    assert TeamPlanner._classify_role("实现一个缓存函数") == "coder"


def test_team_planner_routes_missing_role_by_classification():
    planner = TeamPlanner(llm=MockLLM(), roles=["coder", "reviewer", "tester"])
    raw = ('[{"instruction": "审查模块", "dependencies": []},'
           ' {"instruction": "实现模块", "dependencies": [0]}]')
    tasks = planner._parse(raw)
    assert tasks[0].role == "reviewer"
    assert tasks[1].role == "coder"


@pytest.mark.asyncio
async def test_dispatch_routes_unconfigured_role_with_fallback(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    dl = DecisionLogger()
    # 自定义 planner 直接产出「未知角色」任务，触发派发层自动路由回退
    class WeirdPlanner:
        async def plan(self, prompt):
            return [Task(id="s0", instruction="实现一个功能", role="ghost-worker")]
    orch = OrchestratorAgent(
        config=cfg, blackboard=bb, decision_logger=dl,
        workers=make_workers(cfg, bb,
                             ScriptedWorkerLLM('{"final_answer": "功能完成"}'),
                             ScriptedWorkerLLM(PASS_VERDICT)),
        planner=WeirdPlanner(),
        concurrency=1,
    )
    result = await orch.run("实现一个功能")
    assert result.ok
    assert "功能完成" in result.final_answer
    routed = [dp for dp in dl.decisions if dp.name == "role.routing"]
    assert routed, "决策日志应记录 role.routing"


# ---- 4. 评审耗尽升级介入 ----

@pytest.mark.asyncio
async def test_review_exhausted_upgrades_to_intervention(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    dl = DecisionLogger()
    plan_llm = plan_llm_for(
        {"instruction": "实现模块", "role": "coder", "dependencies": []},
        {"instruction": "审查模块", "role": "reviewer", "dependencies": [0]},
    )
    orch = make_orchestrator(
        cfg, bb,
        make_workers(cfg, bb,
                     ScriptedWorkerLLM(*['{"final_answer": "实现"}'] * 3),
                     ScriptedWorkerLLM(*[RETRY_VERDICT] * 3)),
        plan_llm,
        dl=dl,
        max_review_retries=2,
    )
    result = await orch.run("实现模块")
    assert result.ok is False
    assert result.needs_intervention is True
    assert "需人工介入" in result.final_answer
    assert any(dp.name == "review.exhausted" for dp in dl.decisions)


# ---- 5. 缺陷注入闭环：Coder 硬编码密码 -> Reviewer 驳回 -> Coder 修复 ----

@pytest.mark.asyncio
async def test_injected_defect_caught_by_reviewer_and_fixed(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    plan_llm = plan_llm_for(
        {"instruction": "实现配置模块 config.py", "role": "coder", "dependencies": []},
        {"instruction": "审查配置模块", "role": "reviewer", "dependencies": [0]},
    )
    buggy_write = ('{"tool": "file_ops", "params": {"action": "write", "path": "config.py", '
                   '"content": "password = \\"hardcoded123\\""}}')
    fixed_write = ('{"tool": "file_ops", "params": {"action": "write", "path": "config.py", '
                   '"content": "import os\\npassword = os.environ[\\"DB_PASSWORD\\"]"}}')
    coder_llm = ScriptedWorkerLLM(buggy_write, '{"final_answer": "已实现配置模块"}',
                                  fixed_write, '{"final_answer": "已修复硬编码"}')
    reviewer_llm = ScriptedWorkerLLM(RETRY_VERDICT, PASS_VERDICT)
    orch = make_orchestrator(
        cfg, bb,
        make_workers(cfg, bb, coder_llm, reviewer_llm),
        plan_llm,
    )
    result = await orch.run("实现配置模块")
    assert result.ok
    assert [r["verdict"] for r in result.review_log] == ["retry", "pass"]
    # Reviewer 驳回后 Coder 带反馈重写（评审反馈进入重试任务指令）
    retried = [t for t in result.subtasks
               if t["role"] == "coder" and "[评审反馈]" in t["instruction"]]
    assert len(retried) == 1
    assert "使用环境变量存储密码" in retried[0]["instruction"]
    # 黑板中最终产出为修复后的内容（跨 Agent 引用：Reviewer 只读挂载 Coder 产物）
    artifacts = result.artifacts
    final_coder = next(t for t in result.subtasks
                       if t["role"] == "coder" and "[评审反馈]" in t["instruction"])
    artifact = artifacts.get(f"task:{final_coder['id']}", {})
    content = (artifact.get("files") or {}).get("config.py", "")
    assert "os.environ" in content
    assert "hardcoded123" not in content
    # Reviewer 消息带 REVIEW_RESULT，两次评审各一条
    review_msgs = [m for m in result.messages if m["type"] == "review_result"]
    assert len(review_msgs) == 2