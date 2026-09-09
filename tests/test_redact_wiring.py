# -*- coding: utf-8 -*-
"""脱敏工具接入主流程回归：CLI 输出 + 统一错误出口不泄漏密钥。

覆盖：
- run_cli：--output json / text 的最终答复与失败 error 文本中的密钥形态
  （sk- 长串 / Bearer）在 stdout / stderr 均被脱敏，普通文本不受影响；
- write_error_log / print_error：异常消息、上下文与 traceback 落盘和打印
  内容均不含真实密钥；
- 脱敏发生在 payload 组装之后、_emit 之前，payload 结构与退出码语义不变。

运行：.venv/Scripts/python.exe -X utf8 -m pytest -p no:cacheprovider
      tests/test_redact_wiring.py -q
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent import __main__ as cli
from agent.config import AppConfig
from agent.core.loop import AgentLoop, LoopResult
from agent.core.state import AgentPhase
from agent.errorlog import print_error, write_error_log

REDACTED = "***REDACTED***"
SK_KEY = "sk-" + "aB3cD5eF7gH9jK1lM2nP4qR6sT8uV0wX2yZ4aB3cD5eF7gH"


def write_cfg(root: Path) -> str:
    """离线 mock 配置：关闭子进程/记忆/技能等外部依赖。"""
    body = (
        "agent:\n"
        "  max_rounds: 5\n"
        "  max_retries: 0\n"
        "  max_concurrency: 1\n"
        "  snapshot_enabled: false\n"
        "  auto_testgen: false\n"
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
        "plugin:\n"
        "  enabled: false\n"
        'context:\n  archive_dir: "%s/logs/archives"\n' % root.as_posix()
    )
    cfg_path = root / "mock_cli.yaml"
    cfg_path.write_text(body, encoding="utf-8")
    return str(cfg_path)


def make_cli_args(config: str, workspace: str, **over) -> argparse.Namespace:
    base = {
        "command": "run", "prompt": "测试脱敏任务", "config": config,
        "workspace": workspace, "output": "json", "timeout": None,
        "max_cost": None, "cost_per_1k_tokens": cli.DEFAULT_COST_PER_1K,
        "max_tokens": None, "disable_docker": True, "enable_mcp": False,
        "dry_run": False, "resume": False, "self_check": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


class ScriptedRunLoop(AgentLoop):
    """覆写 run：直接返回携带密钥文本的最终答复，避免真实 LLM 调用。"""

    def __init__(self, cfg: AppConfig, final_answer: str,
                 phase: AgentPhase = AgentPhase.COMPLETED):
        super().__init__(config=cfg)
        self._answer = final_answer
        self._phase = phase

    async def run(self, prompt: str, resume: bool = False,
                  dry_run: bool = False) -> LoopResult:
        return LoopResult(final_answer=self._answer,
                          phase=self._phase, total_rounds=1)


def test_cli_json_output_redacts_secret_final_answer(ws_tmp, capsys):
    cfg_path = write_cfg(ws_tmp)
    workspace = ws_tmp / "ws"
    answer = "登录成功，携带的密钥为 %s，请勿外泄。" % SK_KEY

    def factory(cfg: AppConfig) -> AgentLoop:
        return ScriptedRunLoop(cfg, answer)

    args = make_cli_args(cfg_path, str(workspace), output="json")
    assert cli.run_cli(args, loop_factory=factory) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert SK_KEY not in out
    payload = json.loads(out)
    assert payload["status"] == "completed"
    assert REDACTED in payload["final_answer"]
    assert payload["exit_code"] == 0


def test_cli_text_error_output_redacts_secret(ws_tmp, capsys):
    cfg_path = write_cfg(ws_tmp)
    workspace = ws_tmp / "ws"
    answer = "调用失败：Authorization 头 Bearer %s 无效。" % SK_KEY

    def factory(cfg: AppConfig) -> AgentLoop:
        return ScriptedRunLoop(cfg, answer, phase=AgentPhase.FAILED)

    args = make_cli_args(cfg_path, str(workspace), output="text")
    assert cli.run_cli(args, loop_factory=factory) == cli.EXIT_FAILED
    captured = capsys.readouterr()
    assert SK_KEY not in captured.out and SK_KEY not in captured.err
    assert REDACTED in captured.out    # 文本正文的最终答复
    assert REDACTED in captured.err    # stderr 的 [failed] 错误行


def test_errorlog_file_and_stderr_redact_secret(ws_tmp, capsys):
    log_dir = ws_tmp / "logs"
    secret_line = "连接被拒：key=%s" % SK_KEY

    try:
        raise RuntimeError(secret_line)
    except RuntimeError as exc:
        path = write_error_log(exc, context={"task": "t0",
                                             "detail": secret_line},
                               log_dir=str(log_dir))
    assert path
    text = Path(path).read_text(encoding="utf-8")
    assert SK_KEY not in text
    assert text.count(REDACTED) >= 2   # 异常消息与 context 值各至少一处

    try:
        raise RuntimeError(secret_line)
    except RuntimeError as exc:
        print_error(exc, context={"task": "t0"}, log_path=path)
    err = capsys.readouterr().err
    assert SK_KEY not in err
    assert REDACTED in err