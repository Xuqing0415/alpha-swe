# -*- coding: utf-8 -*-
"""任务A：输入/状态机/配置/快照恢复 边界加固测试。

覆盖：
- 超长任务描述（> MAX_PROMPT_CHARS）在 read_prompt 阶段被拒，UsageError
  （CLI 退出码 2 映射见 EXIT_INTERRUPT）；恰好在长度上限时放行。
- 非法状态机转移（IDLE 跳跃到 RUNNING / COMPLETED 终态后再推进 /
  原地重复推进）均抛 ValueError。
- resume 遇到垃圾 JSON / 缺字段 / 非法字段值的快照时优雅降级：不崩溃、
  回退重新规划并写决策记录（resume.no_snapshot / resume.fallback）。
- 配置错误字段类型 / 越界取值时 load_config 降级到默认配置不崩溃，
  并登记 CONFIG_FALLBACKS；显式坏路径能回落到下一层配置。
"""
import argparse
import io
import json
from pathlib import Path

import pytest

from agent import __main__ as cli
from agent.config import (AgentConfig, AppConfig, MCPOptions,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.state import AgentPhase, StateMachine
from agent.core.task import Task, TaskStatus
from agent.llm import MockLLM


class ScriptedLLM(MockLLM):
    """按脚本依次返回响应；调用次数超出脚本即失败。"""

    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class CountingPlanner:
    """每次都重新规划并记录调用次数（损坏快照必须触发重新规划）。"""

    def __init__(self):
        self.calls = 0

    async def plan(self, prompt, context=""):
        self.calls += 1
        return [Task(id="t0", instruction=prompt, max_retries=0)]


def _loop_config(ws_tmp: Path) -> AppConfig:
    return AppConfig(
        agent=AgentConfig(
            max_rounds=10, max_retries=2, max_concurrency=1,
            snapshot_enabled=True,
            snapshot_dir=str(ws_tmp / "snapshots"),
            snapshot_keep=2,
        ),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


def _write_snapshot(cfg: AppConfig, text: str) -> Path:
    snap_dir = Path(cfg.agent.snapshot_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    path = snap_dir / "task_20260908-000001_step1.json"
    path.write_text(text, encoding="utf-8")
    return path


def _decision_names(loop: AgentLoop):
    return [r.get("name") for r in loop._decision.records()]


# ---- 任务描述边界 ----
def test_read_prompt_rejects_overlong_positional():
    args = argparse.Namespace(prompt="t" * (cli.MAX_PROMPT_CHARS + 1))
    with pytest.raises(cli.UsageError) as exc:
        cli.read_prompt(args)
    msg = str(exc.value)
    assert "任务描述过长" in msg
    assert str(cli.MAX_PROMPT_CHARS) in msg


def test_read_prompt_accepts_prompt_at_length_limit():
    args = argparse.Namespace(prompt="t" * cli.MAX_PROMPT_CHARS)
    prompt = cli.read_prompt(args)
    assert len(prompt) == cli.MAX_PROMPT_CHARS


def test_read_prompt_rejects_overlong_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin",
                        io.StringIO("t" * (cli.MAX_PROMPT_CHARS + 1)))
    args = argparse.Namespace(prompt=None)
    with pytest.raises(cli.UsageError) as exc:
        cli.read_prompt(args)
    assert "任务描述过长" in str(exc.value)


def test_prompt_usage_error_maps_to_exit_code_2():
    # run_cli 把 read_prompt 的 UsageError 转为 EXIT_INTERRUPT（退出码 2）
    assert cli.EXIT_INTERRUPT == 2


# ---- 状态机非法转移 ----
def test_state_machine_rejects_idle_to_running_jump():
    sm = StateMachine()
    assert sm.phase == AgentPhase.IDLE
    assert not sm.can_transition(AgentPhase.RUNNING)
    with pytest.raises(ValueError):
        sm.transition(AgentPhase.RUNNING)
    assert sm.phase == AgentPhase.IDLE  # 拒绝后状态不变


def test_state_machine_is_terminal_after_completed():
    sm = StateMachine()
    # 合法路径推进到终态：IDLE->PLANNING->READY->RUNNING->WAITING->READY->RUNNING->COMPLETED
    for target in (AgentPhase.PLANNING, AgentPhase.READY, AgentPhase.RUNNING,
                   AgentPhase.WAITING, AgentPhase.READY, AgentPhase.RUNNING,
                   AgentPhase.COMPLETED):
        sm.transition(target)
    assert sm.phase == AgentPhase.COMPLETED
    for target in (AgentPhase.PLANNING, AgentPhase.READY, AgentPhase.RUNNING,
                   AgentPhase.WAITING, AgentPhase.COMPLETED,
                   AgentPhase.FAILED):
        with pytest.raises(ValueError):
            sm.transition(target)
    assert sm.phase == AgentPhase.COMPLETED


def test_state_machine_rejects_repeated_transition():
    sm = StateMachine()
    sm.transition(AgentPhase.PLANNING)
    with pytest.raises(ValueError):
        sm.transition(AgentPhase.PLANNING)  # 原地重复推进非法
    assert sm.phase == AgentPhase.PLANNING


# ---- resume / 快照恢复 ----
@pytest.mark.asyncio
async def test_resume_corrupt_json_snapshot_replans(ws_tmp):
    """快照文件内容为垃圾 JSON：不崩溃，降级为无快照并重新规划。"""
    cfg = _loop_config(ws_tmp)
    _write_snapshot(cfg, "{ 这不是合法 JSON ]")
    planner = CountingPlanner()
    loop = AgentLoop(config=cfg,
                     llm=ScriptedLLM('{"final_answer": "损坏快照降级完成"}'),
                     planner=planner)
    result = await loop.run("损坏快照续跑", resume=True)
    assert result.ok
    assert planner.calls == 1, "损坏快照应回退到重新规划"
    assert loop.scheduler.dag.get("t0").status == TaskStatus.COMPLETED
    names = _decision_names(loop)
    assert "resume.no_snapshot" in names
    assert "resume.restored" not in names


@pytest.mark.asyncio
async def test_resume_snapshot_missing_fields_replans(ws_tmp):
    """快照字段缺失（如 tasks 键缺失）：降级为无快照并重新规划。"""
    cfg = _loop_config(ws_tmp)
    _write_snapshot(cfg, json.dumps({"version": 1,
                                     "created_at": "2026-09-08T00:00:00"}))
    planner = CountingPlanner()
    loop = AgentLoop(config=cfg,
                     llm=ScriptedLLM('{"final_answer": "缺字段快照降级完成"}'),
                     planner=planner)
    result = await loop.run("缺字段快照续跑", resume=True)
    assert result.ok
    assert planner.calls == 1
    assert loop.scheduler.dag.get("t0").status == TaskStatus.COMPLETED
    names = _decision_names(loop)
    assert "resume.no_snapshot" in names
    assert "resume.restored" not in names


@pytest.mark.asyncio
async def test_resume_snapshot_invalid_task_field_replans(ws_tmp):
    """快照任务含非法字段值：from_snapshot 抛错被兜住，resume.fallback。"""
    cfg = _loop_config(ws_tmp)
    body = {"version": 1, "tasks": [{"id": "x", "instruction": "坏任务",
                                     "status": "not-a-status"}]}
    _write_snapshot(cfg, json.dumps(body))
    planner = CountingPlanner()
    loop = AgentLoop(config=cfg,
                     llm=ScriptedLLM('{"final_answer": "坏字段快照降级完成"}'),
                     planner=planner)
    result = await loop.run("坏字段快照续跑", resume=True)
    assert result.ok
    assert planner.calls == 1
    assert loop.scheduler.dag.get("t0").status == TaskStatus.COMPLETED
    names = _decision_names(loop)
    assert "resume.fallback" in names


# ---- 配置错误类型 / 越界值降级 ----
def test_load_config_wrong_field_type_degrades(ws_tmp, monkeypatch):
    from agent import config as config_mod

    probe = ws_tmp / "bad_type_agent.yaml"
    probe.write_text("agent:\n  max_rounds: abc\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", probe)
    before = len(config_mod.CONFIG_FALLBACKS)
    cfg = config_mod.load_config()
    assert isinstance(cfg, AppConfig)
    assert cfg.agent.max_rounds == 30  # 类型错误 -> 内置默认
    entries = config_mod.CONFIG_FALLBACKS[before:]
    assert any(n.get("path", "").endswith(probe.name)
               and "非法" in n.get("reason", "") for n in entries)


def test_load_config_out_of_range_value_degrades(ws_tmp, monkeypatch):
    from agent import config as config_mod

    probe = ws_tmp / "bad_range_agent.yaml"
    probe.write_text("llm:\n  temperature: 99.0\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", probe)
    before = len(config_mod.CONFIG_FALLBACKS)
    cfg = config_mod.load_config()
    assert isinstance(cfg, AppConfig)
    assert cfg.llm.temperature == 0.2  # 越界取值 -> 默认温度
    entries = config_mod.CONFIG_FALLBACKS[before:]
    assert any(n.get("path", "").endswith(probe.name)
               and "非法" in n.get("reason", "") for n in entries)


def test_load_config_broken_explicit_path_falls_through(ws_tmp, monkeypatch):
    """显式路径字段损坏：不崩溃，回落到下一层（项目 config/agent.yaml）。"""
    from agent import config as config_mod

    probe = ws_tmp / "broken_explicit.yaml"
    probe.write_text("sandbox:\n  docker_enabled: not-a-bool\n",
                     encoding="utf-8")
    before = len(config_mod.CONFIG_FALLBACKS)
    cfg = config_mod.load_config(str(probe))
    assert isinstance(cfg, AppConfig)
    # 项目 config/agent.yaml 显式 docker_enabled: true（区别于内置默认 False）
    assert cfg.sandbox.docker_enabled is True
    entries = config_mod.CONFIG_FALLBACKS[before:]
    assert any(n.get("path", "").endswith(probe.name)
               and "非法" in n.get("reason", "") for n in entries)
