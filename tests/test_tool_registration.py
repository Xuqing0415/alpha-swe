"""P3 工具接入循环层：注册、超时映射、确认规则。"""
from pathlib import Path

from agent.config import AgentConfig, AppConfig, MemoryConfig, MCPOptions, SandboxConfig
from agent.core.loop import AgentLoop
from agent.llm import MockLLM


class StubPlanner:
    async def plan(self, prompt, context=""):
        return []


def make_config(ws_tmp: Path):
    return AppConfig(
        agent=AgentConfig(max_rounds=5, max_retries=1, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


def test_default_tools_registered(ws_tmp):
    loop = AgentLoop(config=make_config(ws_tmp), llm=MockLLM(),
                     planner=StubPlanner())
    names = set(loop.tools.names())
    assert {"terminal_execute", "file_ops", "run_tests",
            "background_task", "git_ops"} <= names


def test_confirmation_rules(ws_tmp):
    cfg = make_config(ws_tmp)
    cfg.agent.require_confirmation = [
        "file_write", "git_commit", "git_push", "git_branch_delete",
    ]
    cfg.agent.auto_approve = [
        "git_status", "git_diff", "git_log", "git_branch",
    ]
    loop = AgentLoop(config=cfg, llm=MockLLM(), planner=StubPlanner())

    # git 写操作需要确认
    assert loop._needs_confirmation("git_ops", {"action": "commit"}) == "git_commit"
    assert loop._needs_confirmation("git_ops", {"action": "push"}) == "git_push"
    assert loop._needs_confirmation("git_ops", {"action": "branch_delete"}) == "git_branch_delete"
    # git 只读操作自动批准
    assert loop._needs_confirmation("git_ops", {"action": "status"}) is None
    assert loop._needs_confirmation("git_ops", {"action": "diff"}) is None
    # file edit 归入 file_write 确认
    assert loop._needs_confirmation("file_ops",
                                    {"action": "edit", "path": "x.py"}) == "file_write"
    # 后台任务管理无需确认
    assert loop._needs_confirmation("background_task", {"action": "start"}) is None


def test_tool_timeouts(ws_tmp):
    loop = AgentLoop(config=make_config(ws_tmp), llm=MockLLM(),
                     planner=StubPlanner())
    assert loop._tool_timeout("background_task", {}) == 10.0
    assert loop._tool_timeout("git_ops", {}) == 30.0
    assert loop._tool_timeout("file_ops", {"action": "edit"}) == 10.0
    assert loop._tool_timeout("file_ops", {"action": "write"}) == 10.0
    assert loop._tool_timeout("file_ops", {"action": "read"}) == 5.0
    assert loop._tool_timeout("terminal_execute", {}) is None