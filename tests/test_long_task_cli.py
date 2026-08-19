"""收敛期 P1：CLI 标准化（``python -m agent run``）+ 长任务端到端验证。

覆盖（对应收敛计划：阶段二 3.1 CLI 标准化 / 阶段一 2.3 长任务验证）：
- 参数解析、配置覆盖（workspace / docker / mcp / token 上限）、stdin 任务描述；
- JSON 输出 schema 稳定性（ok/status/rounds/tasks/llm_calls/tokens/
  cost_est/files_modified/exit_code）；
- 退出码映射：0 成功 / 1 失败 / 3 超时 / 4 预算超限；
- 长任务：多轮 file_ops 写入 + 上下文自动压缩触发 + 事件流断言。

运行：python -X utf8 -m pytest tests/test_long_task_cli.py -q
"""
import argparse
import asyncio
import io
import json
import textwrap

import pytest

from agent import __main__ as cli
from agent.config import AppConfig
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM


class StubPlanner:
    """固定返回单个 critical 任务，避免消耗脚本化 LLM 的响应。"""

    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


class ScriptedLLM(MockLLM):
    """按脚本依次返回响应；调用次数超出即失败（暴露未脚本化的调用点）。"""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class GatedLLM(MockLLM):
    """阻塞在第一次 LLM 调用上直到被取消（用于超时/预算熔断测试）。"""

    def __init__(self):
        self.gate = asyncio.Event()
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        await self.gate.wait()
        return '{"final_answer": "done"}'


def write_mock_config(root, max_tokens: int = 200) -> str:
    """写一份离线 mock 配置（memory=none 避免嵌入模型加载）。"""
    cfg_path = root / "mock_agent.yaml"
    cfg_path.write_text(textwrap.dedent(f"""\
        agent:
          max_rounds: 10
          max_retries: 2
          max_concurrency: 1
          keep_recent_rounds: 3
        sandbox:
          workspace: "./ws"
          docker_enabled: false
        memory:
          backend: none
        llm:
          provider: mock
        mcp:
          enabled: false
        context:
          max_tokens: {max_tokens}
          compression_threshold: 0.5
          archive_dir: "{root.as_posix()}/logs/archives"
          output_truncate: 2000
        """), encoding="utf-8")
    return str(cfg_path)


def make_cli_args(config: str, workspace: str, **over) -> argparse.Namespace:
    """构造 CLI 参数命名空间（等价于 parse_args 的结果）。"""
    base = {
        "command": "run", "prompt": "测试任务", "config": config, "workspace": workspace,
        "output": "json", "timeout": None, "max_cost": None,
        "cost_per_1k_tokens": cli.DEFAULT_COST_PER_1K, "max_tokens": None,
        "disable_docker": True, "enable_mcp": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


# ---- 辅助函数单测 ----

def test_estimate_cost():
    assert cli.estimate_cost({}, 0.002) == 0.0
    assert cli.estimate_cost({"token_usage": 1000}, 0.002) == 0.002
    assert cli.estimate_cost({"token_usage": 1000.0}, 0.01) == 0.01


def test_extract_files_modified_dedup_and_filter():
    events = [
        {"type": "tool_call",
         "data": {"tool": "file_ops", "success": True,
                  "params": {"action": "write", "path": "a.txt"}}},
        {"type": "tool_call",
         "data": {"tool": "file_ops", "success": True,
                  "params": {"action": "edit", "path": "a.txt"}}},
        {"type": "tool_call",
         "data": {"tool": "file_ops", "success": False,
                  "params": {"action": "write", "path": "b.txt"}}},
        {"type": "tool_call",
         "data": {"tool": "terminal_execute", "success": True,
                  "params": {"command": "dir"}}},
        {"type": "think", "data": {"content": "x"}},
    ]
    assert cli.extract_files_modified(events) == ["a.txt"]


def test_read_prompt_positional():
    assert cli.read_prompt(argparse.Namespace(prompt="显式任务")) == "显式任务"


def test_read_prompt_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("来自 stdin 的任务"))
    assert cli.read_prompt(argparse.Namespace(prompt=None)) == "来自 stdin 的任务"


def test_read_prompt_empty_stdin_raises(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("   "))
    with pytest.raises(cli.UsageError):
        cli.read_prompt(argparse.Namespace(prompt=None))


def test_parse_args_defaults():
    args = cli.parse_args(["run", "任务"])
    assert args.command == "run"
    assert args.prompt == "任务"
    assert args.output == "text"
    assert args.timeout is None
    assert args.max_cost is None
    assert args.enable_mcp is False
    assert args.cost_per_1k_tokens == cli.DEFAULT_COST_PER_1K


def test_build_config_overrides(ws_tmp):
    cfg_path = write_mock_config(ws_tmp)
    args = make_cli_args(cfg_path, str(ws_tmp / "ws2"), max_tokens=150)
    cfg = cli.build_config(args)
    assert cfg.sandbox.workspace.endswith("ws2")
    assert cfg.sandbox.docker_enabled is False
    assert cfg.mcp.enabled is False
    assert cfg.llm.provider.value == "mock"
    assert cfg.context.max_tokens == 150
    assert cfg.agent.max_token_limit == 150


def test_build_config_enable_mcp(ws_tmp):
    cfg_path = write_mock_config(ws_tmp)
    args = make_cli_args(cfg_path, str(ws_tmp / "ws"), enable_mcp=True)
    cfg = cli.build_config(args)
    assert cfg.mcp.enabled is True


def test_main_no_command(capsys):
    assert cli.main([]) == cli.EXIT_OK
    assert "用法" in capsys.readouterr().out


# ---- 长任务端到端（run_cli 内建 asyncio 循环）----

def test_cli_run_json_success_and_files_modified(ws_tmp, capsys):
    cfg_path = write_mock_config(ws_tmp)
    workspace = ws_tmp / "ws"
    created = {}

    def factory(cfg: AppConfig) -> AgentLoop:
        llm = ScriptedLLM(
            '{"think": "' + ("逐步分析任务。 " * 20) + '"}',
            '{"tool": "file_ops", "params": {"action": "write", '
            '"path": "a.txt", "content": "aaa"}}',
            '{"tool": "file_ops", "params": {"action": "write", '
            '"path": "b.txt", "content": "bbb"}}',
            '{"final_answer": "两个文件已写入"}',
        )
        loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
        created["loop"] = loop
        return loop

    args = make_cli_args(cfg_path, str(workspace))
    exit_code = cli.run_cli(args, loop_factory=factory)

    assert exit_code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["exit_code"] == 0
    assert payload["tasks"]["total"] == 1
    assert payload["tasks"]["completed"] == 1
    assert payload["tasks"]["failed"] == 0
    assert payload["rounds"] >= 1
    assert payload["llm_calls"] >= 1
    assert payload["tokens"] > 0
    assert payload["cost_est"] > 0
    assert payload["files_modified"] == ["a.txt", "b.txt"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "aaa"
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "bbb"

    loop = created["loop"]
    run_done = [e for e in loop.events if e.get("type") == "run_done"]
    assert run_done and run_done[0]["data"]["phase"] == "completed"


def test_cli_long_task_triggers_compression(ws_tmp, capsys):
    cfg_path = write_mock_config(ws_tmp, max_tokens=200)
    workspace = ws_tmp / "ws"
    created = {}

    def factory(cfg: AppConfig) -> AgentLoop:
        # 4 次 LLM 调用（think + 3 次写入 + final）累积历史超过压缩阈值
        llm = ScriptedLLM(
            '{"think": "' + ("分析需求并规划步骤。 " * 60) + '"}',
            '{"tool": "file_ops", "params": {"action": "write", '
            '"path": "a.txt", "content": "1"}}',
            '{"tool": "file_ops", "params": {"action": "write", '
            '"path": "b.txt", "content": "2"}}',
            '{"tool": "file_ops", "params": {"action": "write", '
            '"path": "c.txt", "content": "3"}}',
            '{"final_answer": "完成"}',
        )
        loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
        created["loop"] = loop
        return loop

    args = make_cli_args(cfg_path, str(workspace))
    exit_code = cli.run_cli(args, loop_factory=factory)
    assert exit_code == cli.EXIT_OK

    loop = created["loop"]
    snap = loop.metrics.snapshot()
    assert snap["counters"].get("compressions", 0) >= 1
    assert loop.context.compression_count >= 1
    assert (workspace / "a.txt").exists()
    assert (workspace / "c.txt").exists()


def test_cli_timeout_exit_3(ws_tmp, capsys):
    cfg_path = write_mock_config(ws_tmp)

    def factory(cfg: AppConfig) -> AgentLoop:
        return AgentLoop(config=cfg, llm=GatedLLM(), planner=StubPlanner())

    args = make_cli_args(cfg_path, str(ws_tmp / "ws"), timeout=0.2)
    exit_code = cli.run_cli(args, loop_factory=factory)
    assert exit_code == cli.EXIT_TIMEOUT
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 3
    assert payload["status"] == "timeout"
    assert payload["ok"] is False
    assert "超时" in payload.get("error", "")


def test_cli_budget_exit_4(ws_tmp, capsys):
    cfg_path = write_mock_config(ws_tmp)

    def factory(cfg: AppConfig) -> AgentLoop:
        loop = AgentLoop(config=cfg, llm=GatedLLM(), planner=StubPlanner())
        # 预置已消耗 token，让预算监控在首个轮询周期就熔断
        loop.metrics.record_token_usage(3000)
        return loop

    args = make_cli_args(cfg_path, str(ws_tmp / "ws"),
                         max_cost=0.001, cost_per_1k_tokens=0.002)
    exit_code = cli.run_cli(args, loop_factory=factory)
    assert exit_code == cli.EXIT_BUDGET
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 4
    assert payload["status"] == "budget"
    assert payload["ok"] is False
    assert "预算" in payload.get("error", "")

def test_build_config_default_workspace_is_cwd(ws_tmp, monkeypatch):
    """未传 --workspace 时 CLI 默认以当前目录为工作区（修复真实任务访问不到项目文件）。"""
    cfg_path = write_mock_config(ws_tmp)
    args = make_cli_args(cfg_path, None)
    proj = ws_tmp / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(proj)
    cfg = cli.build_config(args)
    assert cfg.sandbox.workspace == str(proj.resolve())
