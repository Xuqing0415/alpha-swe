# -*- coding: utf-8 -*-
"""CLI --dry-run / --resume：计划预览与断点续跑。

覆盖：
- --dry-run 只规划不执行：退出码 0、status=dry_run、JSON 携带 plan、工作区零写入；
- --dry-run 与 --resume 互斥（用法错误退出码 2）；
- --resume 从最近快照恢复任务树：第二轮不重新规划（Planner 不再被调用）；
- --resume 无快照时回退重新规划（Planner 调用 >=1 次且任务成功）。
"""
import argparse
import json
from pathlib import Path

from agent import __main__ as cli
from agent.config import AppConfig
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM


class StubPlanner:
    """固定返回单个 critical 任务，并记录调用次数。"""

    def __init__(self):
        self.calls = 0

    async def plan(self, prompt, context=""):
        self.calls += 1
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


class ScriptedLLM(MockLLM):
    """按脚本依次返回响应；调用次数超出脚本即失败。"""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def write_cfg(root: Path, *, snapshot_dir: str = "") -> str:
    """离线 mock 配置：关闭技能/插件/子进程/自进化与工作区续接。"""
    snap = "  snapshot_enabled: false\n"
    if snapshot_dir:
        snap = ("  snapshot_enabled: true\n"
                '  snapshot_dir: "%s"\n' % snapshot_dir)
    body = (
        "agent:\n"
        "  max_rounds: 10\n"
        "  max_retries: 0\n"
        "  max_concurrency: 1\n"
        "  keep_recent_rounds: 3\n"
        + snap
        + "  auto_testgen: false\n"
        "  regression_check_enabled: false\n"
        "  mutation_check_enabled: false\n"
        "  counterfactual_enabled: false\n"
        "  self_improve_enabled: false\n"
        "  state_tracker_enabled: false\n"
        "  workspace_context_enabled: false\n"
        "sandbox:\n"
        "  workspace: ./ws\n"
        "  docker_enabled: false\n"
        "memory:\n"
        "  backend: none\n"
        "llm:\n"
        "  provider: mock\n"
        "mcp:\n"
        "  enabled: false\n"
        "skills:\n"
        "  enabled: false\n"
        "  workflow_enabled: false\n"
        "plugin:\n"
        "  enabled: false\n"
        "context:\n"
        '  archive_dir: "%s/logs/archives"\n' % root.as_posix()
    )
    cfg_path = root / "mock_cli.yaml"
    cfg_path.write_text(body, encoding="utf-8")
    return str(cfg_path)


def make_cli_args(config: str, workspace: str, **over) -> argparse.Namespace:
    base = {
        "command": "run", "prompt": "测试任务", "config": config,
        "workspace": workspace, "output": "json", "timeout": None,
        "max_cost": None, "cost_per_1k_tokens": cli.DEFAULT_COST_PER_1K,
        "max_tokens": None, "disable_docker": True, "enable_mcp": False,
        "dry_run": False, "resume": False, "self_check": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_dry_run_returns_plan_without_executing(ws_tmp, capsys):
    cfg_path = write_cfg(ws_tmp)
    workspace = ws_tmp / "ws"
    created = {}

    def factory(cfg: AppConfig) -> AgentLoop:
        llm = ScriptedLLM('{"final_answer": "不应被调用的 LLM"}')
        planner = StubPlanner()
        loop = AgentLoop(config=cfg, llm=llm, planner=planner)
        created["planner"] = planner
        created["llm"] = llm
        created["loop"] = loop
        return loop

    args = make_cli_args(cfg_path, str(workspace), dry_run=True)
    exit_code = cli.run_cli(args, loop_factory=factory)
    assert exit_code == cli.EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry_run"
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["plan"], "dry-run JSON 应携带 plan"
    assert payload["plan"][0]["id"] == "t0"
    assert "dry-run" in payload["final_answer"].lower()
    ws_files = list(workspace.glob("**/*")) if workspace.exists() else []
    assert ws_files == [], "dry-run 不应写任何文件"
    loop = created["loop"]
    assert any(e.get("type") == "dry_run_plan" for e in loop.events)
    assert created["llm"].calls == [], "dry-run 不应触发 LLM 工具循环"


def test_dry_run_and_resume_mutually_exclusive(ws_tmp, capsys):
    cfg_path = write_cfg(ws_tmp)
    args = make_cli_args(cfg_path, str(ws_tmp / "ws"),
                         dry_run=True, resume=True)
    exit_code = cli.run_cli(args)
    assert exit_code == cli.EXIT_INTERRUPT
    assert "不能同时" in capsys.readouterr().err


def test_resume_restores_snapshot_without_replanning(ws_tmp, capsys):
    cfg_path = write_cfg(ws_tmp, snapshot_dir=(ws_tmp / "snaps").as_posix())
    workspace = ws_tmp / "ws"
    planner = StubPlanner()

    def factory(cfg: AppConfig) -> AgentLoop:
        # 两轮 CLI 复用同一个 Planner 实例，才能断言第二轮未重新规划
        return AgentLoop(config=cfg, planner=planner)

    first = make_cli_args(cfg_path, str(workspace))
    assert cli.run_cli(first, loop_factory=factory) == cli.EXIT_OK
    capsys.readouterr()  # 丢弃第一轮的 stdout，只解析第二轮
    snaps = list((ws_tmp / "snaps").glob("task_*.json"))
    assert snaps, "第一轮应产生任务快照"

    before = planner.calls
    second = make_cli_args(cfg_path, str(workspace), resume=True)
    assert cli.run_cli(second, loop_factory=factory) == cli.EXIT_OK
    assert planner.calls == before, "resume 不应重新规划"
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"


def test_resume_without_snapshot_falls_back_to_replanning(ws_tmp, capsys):
    cfg_path = write_cfg(ws_tmp,
                         snapshot_dir=(ws_tmp / "empty_snaps").as_posix())
    workspace = ws_tmp / "ws"
    planners = {}

    def factory(cfg: AppConfig) -> AgentLoop:
        planner = StubPlanner()
        planners["planner"] = planner
        return AgentLoop(config=cfg, planner=planner)

    args = make_cli_args(cfg_path, str(workspace), resume=True)
    exit_code = cli.run_cli(args, loop_factory=factory)
    assert exit_code == cli.EXIT_OK
    assert planners["planner"].calls >= 1, "无快照时应回退重新规划"
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
