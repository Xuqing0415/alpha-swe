"""多 Agent 协作测试（设计第 8 节）：黑板/消息协议、团队规划、Worker 角色执行、Orchestrator 仲裁。"""
import json
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig, WorkerRoleConfig, load_team_config)
from agent.core.task import Task, TaskDAG
from agent.llm import MockLLM
from agent.multiagent import (Blackboard, Message, MsgType, OrchestratorAgent,
                              TeamPlanner, WorkerAgent)


def make_config(ws_tmp: Path) -> AppConfig:
    return AppConfig(
        agent=AgentConfig(max_rounds=8, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        # 多 Worker 同进程共享长期记忆：固定 hybrid 后端（auto 会选 qdrant 本地目录，无法二次打开）
        memory=MemoryConfig(backend="hybrid", db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),  # 团队测试不连接 MCP 服务器
    )


class ScriptedWorkerLLM(MockLLM):
    """Worker 用 LLM：经验总结器调用返回空对象（走规则回退），其余按脚本顺序消费。"""

    def __init__(self, *responses: str):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        system = messages[0].get("content", "") if messages else ""
        if "经验总结器" in system:
            return "{}"
        assert self._responses, "Worker LLM 脚本响应已耗尽"
        return self._responses.pop(0)


def team_roles():
    roles = {r.name: r for r in load_team_config().roles}
    if not roles:
        roles = {
            "coder": WorkerRoleConfig(name="coder", tools=["file_ops"]),
            "reviewer": WorkerRoleConfig(name="reviewer", tools=["file_ops"]),
            "tester": WorkerRoleConfig(name="tester", tools=["file_ops"]),
        }
    return roles


def make_workers(cfg, bb, coder_llm, reviewer_llm, tester_llm=None):
    roles = team_roles()
    workers = {
        "coder": WorkerAgent(roles["coder"], config=cfg, llm=coder_llm, blackboard=bb),
        "reviewer": WorkerAgent(roles["reviewer"], config=cfg, llm=reviewer_llm, blackboard=bb),
    }
    if tester_llm is not None:
        workers["tester"] = WorkerAgent(roles["tester"], config=cfg, llm=tester_llm, blackboard=bb)
    return workers


def make_orchestrator(cfg, bb, workers, plan_llm, max_review_retries=2):
    return OrchestratorAgent(
        config=cfg,
        blackboard=bb,
        workers=workers,
        planner=TeamPlanner(llm=plan_llm, roles=list(workers.keys())),
        max_review_retries=max_review_retries,
        concurrency=1,
    )


def plan_llm_for(*items):
    return MockLLM(responder=lambda msgs: json.dumps(list(items), ensure_ascii=False))


PASS_VERDICT = '{"final_answer": "{\\"verdict\\": \\"pass\\", \\"suggestion\\": \\"\\"}"}'
RETRY_VERDICT = ('{"final_answer": "{\\"verdict\\": \\"retry\\", '
                 '\\"suggestion\\": \\"补充边界测试\\"}"}')


# ---- 黑板与消息协议 ----

def test_blackboard_publish_subscribe():
    bb = Blackboard()
    seen = []
    bb.subscribe("task:t1", seen.append)
    bb.publish("task:t1", {"ok": True, "files": {"a.py": "x"}})
    assert bb.get("task:t1")["ok"] is True
    assert seen == [{"ok": True, "files": {"a.py": "x"}}]
    assert bb.keys() == ["task:t1"]
    assert bb.get_many(["task:t1", "task:missing"]) == {
        "task:t1": {"ok": True, "files": {"a.py": "x"}}
    }
    assert bb.artifacts()["task:t1"]["ok"] is True


def test_blackboard_message_protocol():
    bb = Blackboard()
    assign = Message(sender="orchestrator", receiver="coder", type=MsgType.TASK_ASSIGN,
                     payload={"task_id": "t1"})
    done = Message(sender="coder", receiver="orchestrator", type=MsgType.TASK_RESULT,
                   payload={"ok": True})
    bb.post(assign)
    bb.post(done)
    assert len(bb.messages()) == 2
    assert bb.find(msg_type=MsgType.TASK_ASSIGN.value) == [assign]
    assert bb.find(sender="coder") == [done]
    summary = bb.summary()
    assert summary["messages"] == 2
    assert summary["by_type"]["task_assign"] == 1
    assert summary["by_type"]["task_result"] == 1
    d = assign.to_dict()
    assert d["type"] == "task_assign" and d["sender"] == "orchestrator"
    assert d["payload"] == {"task_id": "t1"}


# ---- 团队规划 ----

def test_team_planner_parse_roles_and_dependencies():
    planner = TeamPlanner(llm=MockLLM(), roles=["coder", "reviewer", "tester"])
    raw = (
        "```json\n"
        '[{"instruction": "实现", "role": "coder", "dependencies": [], "priority": 1},\n'
        ' {"instruction": "审查", "role": "reviewer", "dependencies": [0]},\n'
        ' {"instruction": "测试", "role": "tester", "dependencies": [0, 1]}]\n'
        "```"
    )
    tasks = planner._parse(raw)
    assert [t.role for t in tasks] == ["coder", "reviewer", "tester"]
    assert tasks[0].id == "s0" and tasks[0].priority == 1
    assert tasks[1].dependencies == ["s0"]
    assert tasks[2].dependencies == ["s0", "s1"]


def test_team_planner_unknown_role_normalized_to_coder():
    planner = TeamPlanner(llm=MockLLM(), roles=["coder"])
    tasks = planner._parse('[{"instruction": "x", "role": "janitor"}]')
    assert len(tasks) == 1 and tasks[0].role == "coder"


@pytest.mark.asyncio
async def test_team_planner_fallback_on_garbage():
    llm = MockLLM(responder=lambda msgs: "这不是 JSON")
    planner = TeamPlanner(llm=llm, roles=["coder", "reviewer"])
    tasks = await planner.plan("修复 bug")
    assert len(tasks) == 1
    assert tasks[0].role == "coder"
    assert tasks[0].instruction == "修复 bug"


def test_task_dag_create_task_with_role():
    dag = TaskDAG()
    t = dag.create_task(instruction="实现", role="coder")
    assert t.role == "coder"
    assert dag.get(t.id) is t


# ---- Worker 角色执行 ----

@pytest.mark.asyncio
async def test_worker_agent_role_execution_and_artifact(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    role = WorkerRoleConfig(name="coder", system_prompt="你是团队中的 Coder。",
                            tools=["file_ops"])
    llm = ScriptedWorkerLLM(
        '{"tool": "file_ops", "params": {"action": "write", "path": "calc.py", '
        '"content": "def add(a, b):\\n    return a + b\\n"}}',
        '{"final_answer": "已写入 calc.py"}',
    )
    worker = WorkerAgent(role, config=cfg, llm=llm, blackboard=bb)
    result = await worker.execute_task(Task(id="t1", instruction="编写 calc.py"))
    assert result.ok
    assert result.output == "已写入 calc.py"
    assert result.rounds >= 1
    artifact = bb.get("task:t1")
    assert artifact is not None and artifact["ok"] is True
    assert artifact["files"]["calc.py"] == "def add(a, b):\n    return a + b\n"
    target = ws_tmp / "ws" / "calc.py"
    assert target.read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"


# ---- Orchestrator 全流程 ----

@pytest.mark.asyncio
async def test_orchestrator_success_path(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    plan_llm = plan_llm_for(
        {"instruction": "实现 add 函数", "role": "coder", "dependencies": []},
        {"instruction": "审查 add 函数", "role": "reviewer", "dependencies": [0]},
    )
    orch = make_orchestrator(
        cfg, bb,
        make_workers(cfg, bb,
                     ScriptedWorkerLLM('{"final_answer": "已实现 add"}'),
                     ScriptedWorkerLLM(PASS_VERDICT)),
        plan_llm,
    )
    result = await orch.run("实现 add 函数")
    assert result.ok
    assert "已实现 add" in result.final_answer
    assert len(result.review_log) == 1
    assert result.review_log[0]["verdict"] == "pass"
    types = {m["type"] for m in result.messages}
    assert {"task_assign", "task_result", "review_result"} <= types
    assert {t["role"] for t in result.subtasks} == {"coder", "reviewer"}
    assert result.blackboard_summary["artifacts"] == 2


@pytest.mark.asyncio
async def test_orchestrator_review_retry_then_pass(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    plan_llm = plan_llm_for(
        {"instruction": "实现模块", "role": "coder", "dependencies": []},
        {"instruction": "审查模块", "role": "reviewer", "dependencies": [0]},
    )
    orch = make_orchestrator(
        cfg, bb,
        make_workers(cfg, bb,
                     ScriptedWorkerLLM('{"final_answer": "实现 v1"}',
                                       '{"final_answer": "实现 v2"}'),
                     ScriptedWorkerLLM(RETRY_VERDICT, PASS_VERDICT)),
        plan_llm,
    )
    result = await orch.run("实现模块")
    assert result.ok
    assert len(result.review_log) == 2
    assert [r["verdict"] for r in result.review_log] == ["retry", "pass"]
    assert result.review_log[0]["round"] == 0
    assert result.review_log[1]["round"] == 1
    retried = [t for t in result.subtasks
               if t["role"] == "coder" and "[评审反馈]" in t["instruction"]]
    assert len(retried) == 1
    assert "补充边界测试" in retried[0]["instruction"]
    # 重试 coder 保留原始任务的 parent 链（仲裁计数锚点）
    original = next(t for t in result.subtasks if t["role"] == "coder" and t["parent_id"] is None)
    assert retried[0]["parent_id"] == original["id"]


@pytest.mark.asyncio
async def test_orchestrator_review_retry_exhausted(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
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
        max_review_retries=2,
    )
    result = await orch.run("实现模块")
    assert result.ok is False
    assert "评审未通过" in result.final_answer
    assert len(result.review_log) == 3
    assert all(r["verdict"] == "retry" for r in result.review_log)
    assert result.blackboard_summary["messages"] > 0


@pytest.mark.asyncio
async def test_orchestrator_reviewer_parse_failure_fail_open(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    plan_llm = plan_llm_for(
        {"instruction": "实现模块", "role": "coder", "dependencies": []},
        {"instruction": "审查模块", "role": "reviewer", "dependencies": [0]},
    )
    orch = make_orchestrator(
        cfg, bb,
        make_workers(cfg, bb,
                     ScriptedWorkerLLM('{"final_answer": "实现完成"}'),
                     ScriptedWorkerLLM('{"final_answer": "看起来没问题"}')),
        plan_llm,
    )
    result = await orch.run("实现模块")
    assert result.ok
    assert result.review_log[0]["verdict"] == "pass"
    assert result.review_log[0]["suggestion"] == ""


@pytest.mark.asyncio
async def test_orchestrator_three_role_dag(ws_tmp):
    cfg = make_config(ws_tmp)
    bb = Blackboard()
    plan_llm = plan_llm_for(
        {"instruction": "实现模块", "role": "coder", "dependencies": []},
        {"instruction": "审查模块", "role": "reviewer", "dependencies": [0]},
        {"instruction": "测试模块", "role": "tester", "dependencies": [0]},
    )
    orch = make_orchestrator(
        cfg, bb,
        make_workers(cfg, bb,
                     ScriptedWorkerLLM('{"final_answer": "已实现"}'),
                     ScriptedWorkerLLM(PASS_VERDICT),
                     ScriptedWorkerLLM('{"final_answer": "测试通过 3/3"}')),
        plan_llm,
    )
    result = await orch.run("实现并测试模块")
    assert result.ok
    assert "已实现" in result.final_answer
    assert "测试通过 3/3" in result.final_answer
    assert sorted(t["role"] for t in result.subtasks) == ["coder", "reviewer", "tester"]


def test_multiagent_exports():
    from agent.multiagent import (Artifact, ReviewRecord, TeamResult,
                                  WorkerResult)
    assert Artifact is not None
    assert ReviewRecord is not None
    assert TeamResult is not None
    assert WorkerResult is not None