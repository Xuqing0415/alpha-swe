# -*- coding: utf-8 -*-
"""关键操作性能基线（pytest-benchmark，边界打磨第 6 步）。

对以下关键热路径建立耗时基线（Ubuntu chaos-stage 单独 step 运行）：
- 状态机推进：合法转移链的批量推进；
- 输出解析：LLM JSON 工具消息批量解析；
- 命令识别：沙箱策略对混合安全/危险/网络命令的批量判定；
- 路径校验：工作区相对路径的批量解析。

测试内不做耗时断言（避免不同 runner 抖动），改为把耗时写入
--benchmark-json，由 chaos.yml 后续 step 读取 JSON 并按宽松上限断言，
防止数量级性能回归；quality-gate 三平台离线套件显式忽略本模块。

运行：python -X utf8 -m pytest tests/test_perf_baseline.py --benchmark-autosave
"""
from pathlib import Path

from agent.core.state import AgentPhase, StateMachine
from agent.parser.parser import Parser
from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import ExecutionContext
from agent.tools.fileio import resolve_workspace_path

_TOOL_MSG = (
    '{"tool": "file_ops", "params": {"action": "read", '
    '"path": "src/app.py"}, '
    '"reasoning": "先读取目标文件确认现状后再修改"}'
)


def _state_chain():
    sm = StateMachine()
    for target in (AgentPhase.PLANNING, AgentPhase.READY,
                   AgentPhase.RUNNING, AgentPhase.WAITING,
                   AgentPhase.READY, AgentPhase.RUNNING,
                   AgentPhase.COMPLETED):
        sm.transition(target)
    return sm.phase


def test_bench_state_advance(benchmark):
    out = benchmark(lambda: [_state_chain() for _ in range(500)])
    assert len(out) == 500


def test_bench_parser_tool_json(benchmark):
    parser = Parser()
    out = benchmark(lambda: [parser.parse(_TOOL_MSG) for _ in range(300)])
    assert out[-1].action_type == "tool_call"


def test_bench_policy_command_gate(benchmark, ws_tmp):
    policy = SandboxPolicy(workspace=str(ws_tmp), network_enabled=False,
                           network_policy="deny")
    ctx = ExecutionContext(workspace=str(ws_tmp))
    cmds = (["cat src/app.py", "rm -rf /", "curl https://example.com",
             "git status", "pwd; ls", "git push origin master"] * 80)

    def run():
        allowed = 0
        for cmd in cmds:
            ok, _ = policy.check("terminal_execute", {"command": cmd}, ctx)
            if ok:
                allowed += 1
        return allowed

    out = benchmark(run)
    assert out > 0


def test_bench_path_resolve(benchmark, ws_tmp):
    root = str(Path(ws_tmp).resolve())
    paths = ["src/%d/app.py" % i for i in range(40)] + [
        "README.md", "tests/x/y.txt", "a/b/c.log"]

    def run():
        last = None
        for rel in paths:
            last = resolve_workspace_path(root, rel)
        return last

    out = benchmark(run)
    assert str(out).startswith(root)
