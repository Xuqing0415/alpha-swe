# -*- coding: utf-8 -*-
"""工具 / 沙箱命令安全边界加固测试（任务 B）。

覆盖四块（每项先审计现状，缺口处做最小修复并在此锁行为）：
1) agent/tools/terminal.py
   - 子进程显式 argv（无 shell=True）、字面参数（含 ; && | $() 反引号 %0a -n）
     原样到达子进程、绝不被 shell 二次解释（用 python -c 打印 argv 证明）；
   - 超时命令被 terminate->kill 并返回带错误信息的结构化结果；
   - 超大输出按 output_truncate 配置上限截断、原始输出存档；
   - read-only 角色拒绝写语义 / shell 元字符命令、放行白名单命令。
2) agent/tools/fileio.py
   - ..\\ 、..%2f 、..%5c 等穿越变体不会落到工作区外；绝对路径逃逸被拒绝；
   - 精确行编辑不误伤邻行，写入前后快照（before/after）与审计回滚存在。
3) agent/sandbox/policy.py
   - rm 根删除绕过变体（多余空白 / 制表符 / -fr / -rfv / 长选项 / ';' '&&' '|'
     反引号 '$()' bash -c 包装 / 大小写 / 前导空格 / %0a %26%26 编码）全拦截；
   - git status / pwd / ls / echo 等安全命令放行。
4) agent/tools/test_tool.py
   - pytest 可执行缺失、不支持框架、lint/输出解析失败均结构化降级而非裸异常。
"""
import asyncio
import os
import sys

import pytest

from agent.code.test_runner import parse_test_output, run_tests
from agent.tools import test_tool as test_tool_mod
from agent.sandbox.audit import FileAuditStore
from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import ErrorCategory, ExecutionContext
from agent.tools.fileio import FileIOTool, resolve_workspace_path
from agent.tools.terminal import TerminalTool


def _py_run_command(code: str, args=()):
    """构造在终端工具里执行 python -c 的命令（Windows 走 & 调用运算符）。

    参数以 shell 单引号包裹，保证特殊内容只作为字面参数传给子进程。
    """
    exe = sys.executable
    quoted = " ".join("'" + a + "'" for a in args)
    invoke = "'" + exe + "' -c \"" + code + "\""
    if quoted:
        invoke += " " + quoted
    return ("& " + invoke) if os.name == "nt" else invoke


@pytest.fixture(scope="module")
def spawn_pipe():
    """部分沙箱拒绝 asyncio 子进程的 PIPE 句柄（Windows 上 WinError 5），
    此时真实子进程用例整体跳过（与仓库既有 terminal 用例同因）。"""
    if os.name != "nt":
        return True

    async def _probe():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "print(1)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    try:
        asyncio.run(_probe())
        return True
    except OSError as e:
        pytest.skip("当前沙箱禁止 asyncio+PIPE 子进程创建（%s），跳过真实进程用例" % e)


# --------------------------------------------------------------------------
# 1) terminal.py
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_terminal_literal_args_never_shell_interpreted(ws_tmp, spawn_pipe):
    """含 ; && | $() 反引号 %0a -n 的字面参数按原样到达子进程 argv。"""
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = TerminalTool()
    args = ["--", "a;b", "c&&d", "e|f", "g$(h)", "i`j", "k%0al", "-n"]
    code = "import sys; print('ARGV=' + repr(sys.argv[1:]))"
    r = await tool.execute({"command": _py_run_command(code, args),
                            "timeout": 60}, ctx)
    assert r.success, r.error
    # 完全等于字面 argv：任何被 shell 解释 / 拆命令 / 转义丢失都会破坏相等性
    assert r.output == "ARGV=" + repr(list(args))


def test_terminal_build_argv_explicit_list_without_shell_flag():
    """子进程通过显式 argv 列表启动（无 shell=True 语义）。"""
    argv = TerminalTool()._build_argv("echo hi")
    assert isinstance(argv, list) and argv
    assert all(isinstance(a, str) for a in argv)
    if os.name == "nt":
        # powershell -NoProfile -NonInteractive -Command <script>
        assert argv[0].lower().endswith("powershell")
        assert "-Command" in argv
    else:
        assert argv[0].endswith("/bin/sh")
        assert argv[1] == "-c"


@pytest.mark.asyncio
async def test_terminal_read_only_rejects_destructive_and_meta_commands(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = TerminalTool(read_only=True)
    for cmd in ["rm -rf sub", "Remove-Item x", "echo hi; rm x",
                "cat a > b", "pwd && ls", "ls | wc -l", "echo `id`",
                "echo $(pwd)"]:
        r = await tool.execute({"command": cmd, "timeout": 10}, ctx)
        assert r.success is False, cmd
        assert r.error_category == ErrorCategory.PERMISSION, cmd
        assert "只读" in (r.error or "")


@pytest.mark.asyncio
async def test_terminal_read_only_allows_whitelisted_commands(ws_tmp, spawn_pipe):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = TerminalTool(read_only=True)
    r = await tool.execute({"command": "echo hi", "timeout": 30}, ctx)
    assert r.success, r.error
    r2 = await tool.execute({"command": "pwd", "timeout": 30}, ctx)
    assert r2.success, r2.error


@pytest.mark.asyncio
async def test_terminal_timeout_terminates_and_returns_error(ws_tmp, spawn_pipe):
    """长命令在超时后被终止，返回带错误信息的结构化结果（transient）。"""
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = TerminalTool()
    cmd = _py_run_command("import time; time.sleep(30)")
    r = await tool.execute({"command": cmd, "timeout": 1}, ctx)
    assert r.success is False
    assert r.metadata.get("timed_out") is True
    assert "超时" in (r.error or "")
    assert r.error_category == ErrorCategory.TRANSIENT


class _StubPlanner:
    async def plan(self, prompt, context=""):
        from agent.core.task import Task
        return [Task(id="t0", instruction=prompt)]


def _make_loop_config(ws_tmp, truncate):
    from agent.config import (AgentConfig, AppConfig, ContextConfig, MCPOptions,
                              MemoryConfig, SandboxConfig)
    return AppConfig(
        agent=AgentConfig(max_rounds=5, max_retries=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        context=ContextConfig(output_truncate=truncate,
                              archive_dir=str(ws_tmp / "logs" / "archives")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


@pytest.mark.asyncio
async def test_terminal_giant_output_truncated_to_configured_cap(ws_tmp, spawn_pipe):
    """真实 terminal 命令的大输出按 output_truncate 上限截断并完整存档。"""
    from agent.core.loop import AgentLoop
    from agent.llm import MockLLM
    ws = ws_tmp / "ws"
    ws.mkdir()
    loop = AgentLoop(config=_make_loop_config(ws_tmp, truncate=2000),
                     llm=MockLLM(), planner=_StubPlanner())
    ctx = ExecutionContext(workspace=str(ws))
    raw = "A" * 6000
    r = await TerminalTool().execute(
        {"command": _py_run_command("import sys; sys.stdout.write('A' * 6000)"),
         "timeout": 60}, ctx)
    assert r.success, r.error
    assert r.output == raw
    obs = await loop._summarize_observation("terminal_execute", r)
    assert "已压缩" in obs
    assert len(obs) < len(raw)
    outputs = ws_tmp / "logs" / "outputs"
    archived = list(outputs.glob("*.txt")) if outputs.is_dir() else []
    assert any(len(f.read_text(encoding="utf-8")) >= len(raw) for f in archived)


@pytest.mark.asyncio
async def test_terminal_short_output_kept_under_config_cap(ws_tmp, spawn_pipe):
    """短输出未超 output_truncate 时原样保留、不压缩。"""
    from agent.core.loop import AgentLoop
    from agent.llm import MockLLM
    ws = ws_tmp / "ws"
    ws.mkdir()
    loop = AgentLoop(config=_make_loop_config(ws_tmp, truncate=2000),
                     llm=MockLLM(), planner=_StubPlanner())
    ctx = ExecutionContext(workspace=str(ws))
    r = await TerminalTool().execute(
        {"command": _py_run_command("import sys; sys.stdout.write('ok' * 100)"),
         "timeout": 60}, ctx)
    assert r.success, r.error
    text = "[terminal_execute] " + r.output
    obs = await loop._summarize_observation("terminal_execute", r)
    assert obs == text
    assert "已压缩" not in obs


# --------------------------------------------------------------------------
# 2) fileio.py
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fileio_dotdot_encoded_variants_stay_inside_workspace(ws_tmp):
    ws = ws_tmp / "ws"
    ws.mkdir()
    ctx = ExecutionContext(workspace=str(ws))
    tool = FileIOTool()
    # 真实分隔符的 ..\\ / ../ 变体一律拒绝
    for p in [r"..\escape.txt", r"sub/../escape.txt", r"..\..\escape.txt"]:
        r = await tool.execute({"action": "write", "path": p, "content": "x"}, ctx)
        assert r.success is False, p
        assert "路径穿越" in (r.error or "") or "越界" in (r.error or "")
    assert not (ws_tmp / "escape.txt").exists()
    # URL 编码变体不会被文件系统解码，只可能以字面名落在工作区内
    for p in ["..%2fescape.txt", "..%5cescape.txt", "sub%2f..%2fescape.txt"]:
        r = await tool.execute({"action": "write", "path": p, "content": "MARK"}, ctx)
        assert r.success, (p, r.error)
        target = resolve_workspace_path(str(ws), p)
        assert str(target.resolve()).startswith(str(ws.resolve()) + os.sep)
        assert target.read_text(encoding="utf-8") == "MARK"
    # 工作区外层没有被任何变体污染
    outside = [x.name for x in ws_tmp.iterdir() if x.name != "ws"]
    assert outside == []


@pytest.mark.asyncio
async def test_fileio_absolute_path_escape_rejected(ws_tmp):
    ws = ws_tmp / "ws"
    ws.mkdir()
    outside = ws_tmp / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    ctx = ExecutionContext(workspace=str(ws))
    tool = FileIOTool()
    for action in ("read", "write"):
        r = await tool.execute(
            {"action": action, "path": str(outside), "content": "pwn"}, ctx)
        assert r.success is False, (action, r.error)
        assert "越界" in (r.error or "")
    assert outside.read_text(encoding="utf-8") == "secret"
    # 工作区内的绝对路径仍允许
    inside = ws / "ok.txt"
    r = await tool.execute({"action": "write", "path": str(inside),
                            "content": "in"}, ctx)
    assert r.success
    assert inside.read_text(encoding="utf-8") == "in"


@pytest.mark.asyncio
async def test_fileio_platform_system_path_escape_rejected(ws_tmp):
    ws = ws_tmp / "ws"
    ws.mkdir()
    ctx = ExecutionContext(workspace=str(ws))
    tool = FileIOTool()
    candidates = (["C:/Windows/win.ini", "C:\\Windows\\win.ini"]
                  if os.name == "nt" else ["/etc/passwd"])
    for p in candidates:
        r = await tool.execute({"action": "read", "path": p}, ctx)
        assert r.success is False, p
        assert "越界" in (r.error or ""), (p, r.error)


@pytest.mark.asyncio
async def test_fileio_edit_precise_neighbors_and_snapshots(ws_tmp):
    ws = ws_tmp / "ws"
    ws.mkdir()
    audit = FileAuditStore(str(ws_tmp / "audit"))
    tool = FileIOTool(audit_store=audit)
    ctx = ExecutionContext(workspace=str(ws), task_id="t-edit")
    target = ws / "notes.txt"
    orig = "alpha\nbeta\ngamma\ndelta\n"
    target.write_text(orig, encoding="utf-8")
    r = await tool.execute(
        {"action": "edit", "path": "notes.txt",
         "start_line": 2, "end_line": 2, "content": "BETA2"}, ctx)
    assert r.success, r.error
    after = "alpha\nBETA2\ngamma\ndelta\n"
    assert target.read_text(encoding="utf-8") == after  # 邻行未被误伤
    assert r.metadata["diff_before"] == orig
    assert r.metadata["diff_after"] == after
    rows = audit.find(str(target))
    assert len(rows) == 1
    assert rows[0]["before"] == orig
    assert rows[0]["after"] == after
    assert "-beta" in rows[0]["diff"] and "+BETA2" in rows[0]["diff"]
    restored = audit.rollback(str(target))
    assert restored == orig
    assert target.read_text(encoding="utf-8") == orig


# --------------------------------------------------------------------------
# 3) sandbox/policy.py
# --------------------------------------------------------------------------

def _policy():
    return SandboxPolicy(workspace=".",
                         protected_paths=[".git", "config/agent.yaml"])


def test_policy_blocks_rm_root_delete_bypass_variants():
    p = _policy()
    ctx = ExecutionContext(workspace=".")
    payloads = [
        "rm -rf /",              # 原形
        "  rm -rf /",            # 前导空格
        "RM -RF /",              # 大小写混淆
        "rm -rf  /",             # 多余空白
        "rm\t-rf\t/",            # 制表符
        "rm -fr /",              # 选项顺序变体
        "rm -rfv /",             # 选项合并变体
        "rm -fR /",
        "rm -rf -- /",
        "rm --recursive --force /",  # 长选项
        ";rm -rf /",             # 分号前缀
        "echo x && rm -rf /",    # && 连接
        "echo x | rm -rf /",     # 管道
        "`rm -rf /`",            # 反引号
        "$(rm -rf /)",           # $() 命令替换
        'bash -c "rm -rf /"',    # shell 包装
        "rm -rf /;echo y",       # 尾随分号
        "rm -rf  /;echo y",      # 空白 + 尾随分号
        "rm -fr /*",             # 通配根
    ]
    for cmd in payloads:
        ok, reason = p.check("terminal_execute", {"command": cmd}, ctx)
        assert not ok, "漏拦: %r" % cmd
        assert reason


def test_policy_allows_safe_commands():
    p = _policy()
    ctx = ExecutionContext(workspace=".")
    for cmd in ["git status", "git log --oneline -1", "git diff",
                "pwd", "ls -la", "echo hi", "cat notes.txt"]:
        ok, reason = p.check("terminal_execute", {"command": cmd}, ctx)
        assert ok, (cmd, reason)


def test_policy_encoded_or_wrapped_payloads_still_blocked():
    p = _policy()
    ctx = ExecutionContext(workspace=".")
    for cmd in ["echo x%0arm -rf /", "echo x%26%26rm -rf /",
                "echo x%0a&&rm -rf /", "echo x;rm -rf /",
                "echo x&&rm -rf /", "echo x|rm -rf /"]:
        ok, reason = p.check("terminal_execute", {"command": cmd}, ctx)
        assert not ok, "漏拦: %r" % cmd
        assert reason


def test_policy_blocks_chained_delete_of_protected_paths():
    p = _policy()
    ctx = ExecutionContext(workspace=".")
    for cmd in ["rm -rf .git; echo done", "Remove-Item -Recurse .git | Out-Null",
                "rm -rf config && ls"]:
        ok, reason = p.check("terminal_execute", {"command": cmd}, ctx)
        assert not ok, "漏拦: %r" % cmd
        assert "受保护" in reason, (cmd, reason)


# --------------------------------------------------------------------------
# 4) test_tool.py / test_runner.py
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_test_tool_missing_executable_returns_structured_error(ws_tmp, monkeypatch):
    """pytest 可执行缺失时返回结构化/友好错误而非裸异常。"""

    async def _deny_spawn(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "pytest")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _deny_spawn)
    result = await run_tests("pytest", ".", str(ws_tmp), timeout=10)
    assert result.success is False
    assert "找不到测试命令" in result.output
    tool_result = await test_tool_mod.TestRunnerTool(default_timeout=10).execute(
        {"framework": "pytest", "target": "."},
        ExecutionContext(workspace=str(ws_tmp)))
    assert tool_result.success is False
    assert "找不到测试命令" in (tool_result.output or "")
    assert tool_result.metadata.get("failures") == []


@pytest.mark.asyncio
async def test_run_tests_unsupported_framework_structured(ws_tmp):
    result = await run_tests("no_such_framework", "", str(ws_tmp), timeout=10)
    assert result.success is False
    assert "不支持的测试框架" in result.output
    assert result.failures == []


def test_parse_test_output_degrades_on_garbage():
    garbage = ("??? \x00\x01 INTERNALERROR> oops\n"
               "no test markers at all\n") * 3
    assert parse_test_output("pytest", garbage) == []
    assert parse_test_output("go", "\x00\xff not a real go output") == []
    assert parse_test_output("maven", "garbage without markers") == []
    assert parse_test_output("nosuch-framework", "anything") == []


def test_parse_test_output_extracts_pytest_failure():
    out = ("============================= FAILURES =============================\n"
           "_______________________________ test_x _______________________________\n"
           "    def test_x():\n"
           ">       assert 1 == 2\n"
           "E       assert 1 == 2\n"
           "short test summary info\n"
           "FAILED tests/test_demo.py::test_x - assert 1 == 2\n")
    failures = parse_test_output("pytest", out)
    assert failures and failures[0].name == "tests/test_demo.py::test_x"
