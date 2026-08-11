"""TUI 测试：事件格式化、事件订阅、终端实时输出桥接、Textual 无头运行。"""
import asyncio
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.tools.base import ExecutionContext
from agent.tools.terminal import TerminalTool
from tui.formatting import format_event
from tui.app import AlphaSWEApp, CommandInput
from textual.widgets import Input, Static


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp: Path, **kw):
    kw.setdefault("enabled", False)
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(**kw),
    )


# ---- 事件格式化（纯函数） ----
def test_format_event_think():
    text = format_event({"type": "think", "data": {"content": "分析中"}})
    # 设计格式：[HH:MM:SS] THINK 内容
    assert text.plain.startswith("[")
    assert "THINK" in text.plain and "分析中" in text.plain


def test_format_event_tool_call():
    text = format_event({
        "type": "tool_call",
        "data": {"tool": "terminal_execute", "params": {"command": "ls"},
                 "success": True, "output": "a.txt"},
    })
    assert text.plain.startswith("[")
    assert "ACT" in text.plain
    assert "terminal_execute" in text.plain
    assert "成功" in text.plain


def test_format_event_plan_created():
    text = format_event({"type": "plan_created",
                         "data": {"total": 2, "tasks": ["a", "b"]}})
    assert "规划 2 个子任务" in text.plain
    assert "INFO" in text.plain


def test_format_event_error_and_ok_types():
    err = format_event({"type": "run_error", "data": {"error": "boom"}})
    assert "ERROR" in err.plain and "boom" in err.plain
    ok = format_event({"type": "task_done", "data": {"task_id": "t1"}})
    assert "OK" in ok.plain and "t1" in ok.plain


def test_format_event_skills_activated():
    text = format_event({
        "type": "skills_activated",
        "data": {"skills": ["add-rest-endpoint"], "total": 5},
    })
    assert "技能工作流激活" in text.plain
    assert "add-rest-endpoint" in text.plain
    assert "展开 5 个子任务" in text.plain


def test_format_event_task_start_skill_progress():
    text = format_event({
        "type": "task_start",
        "data": {"task_id": "add-rest-endpoint::route",
                 "instruction": "定义 REST 端点路由",
                 "skill": "add-rest-endpoint",
                 "skill_step": "route", "step_index": 0, "step_total": 5},
    })
    assert "1/5" in text.plain and "route" in text.plain


def test_format_event_unknown_type():
    text = format_event({"type": "weird", "data": {"x": 1}})
    assert "weird" in text.plain


# ---- AgentLoop 事件订阅 ----
@pytest.mark.asyncio
async def test_loop_subscribe_receives_events(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "完成"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    seen = []
    loop.subscribe(lambda record: seen.append(record["type"]))
    await loop.run("订阅事件")
    assert "run_start" in seen
    assert "task_start" in seen
    assert "run_done" in seen


# ---- 终端实时输出回调 ----
@pytest.mark.asyncio
async def test_terminal_tool_streams_output(ws_tmp):
    lines = []
    (ws_tmp / "ws").mkdir(parents=True, exist_ok=True)
    tool = TerminalTool(default_timeout=15)
    ctx = ExecutionContext(
        workspace=str(ws_tmp / "ws"),
        output_callback=lambda line: lines.append(line),
    )
    result = await tool.execute(
        {"command": 'python -c "print(\'hello tui\'); print(\'line2\')"'},
        ctx,
    )
    assert result.success
    assert any("hello tui" in line for line in lines)
    assert any("line2" in line for line in lines)


# ---- 会话回放 CLI：通配符展开与缺档报错 ----
def test_replay_glob_expands_latest(ws_tmp):
    import json
    import os
    import time

    from tui.__main__ import replay_session

    arch = {"session_id": "s1", "created_at": "2026-01-01T00:00:00",
            "prompt": "旧会话", "events": [], "spans": [], "decisions": []}
    (ws_tmp / "session_1.json").write_text(
        json.dumps(arch, ensure_ascii=False), encoding="utf-8")
    arch["session_id"] = "s2"
    arch["events"] = [{"type": "think", "ts": 1.0,
                       "data": {"content": "新会话"}}]
    newer = ws_tmp / "session_2.json"
    newer.write_text(json.dumps(arch, ensure_ascii=False), encoding="utf-8")
    os.utime(newer, (time.time() + 1, time.time() + 1))  # 保证 session_2 最新

    # 通配符应展开并回放最新档案（不再报 Errno 22）
    assert replay_session(str(ws_tmp / "session_*.json")) == 0
    # 无匹配档案应明确报错而非抛 OSError
    assert replay_session(str(ws_tmp / "no_such_*.json")) == 1


# ---- Textual 无头运行：事件流入左栏并正常结束 ----
@pytest.mark.asyncio
async def test_tui_streams_events_and_finishes(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"think": "先看看"}',
        '{"final_answer": "TUI 完成"}',
    )
    app = AlphaSWEApp(
        "TUI 测试",
        config=cfg,
        llm=llm,
        planner=StubPlanner(),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        # 轮询等待 Agent 完成（worker 跑在测试事件循环内）
        for _ in range(100):
            await pilot.pause(0.05)
            if app._finished is not None:
                break
        assert app._finished is not None, "Agent 未在预期时间内完成"
        assert app._finished.ok
        # 主日志区应包含 think 与 run_done 事件渲染
        log = app.query_one("#main-log")
        rendered = "".join(str(line) for line in log.lines)
        assert "TUI 完成" in rendered or "任务结束" in rendered
        # Ctrl+P 暂停不应抛错（loop 已结束，为空操作）
        await pilot.press("ctrl+p")
        await pilot.pause()



# ---- 多视图与任务面板（F5 主区视图 + F2/F4 布局） ----
@pytest.mark.asyncio
async def test_tui_cycle_views_and_render(ws_tmp):
    cfg = make_config(ws_tmp)
    cfg.agent.auto_approve = ["file_write"]  # 豁免确认，让 run 不被弹窗阻塞
    llm = ScriptedLLM(
        '{"think": "视图测试"}',
        '{"tool": "file_ops", "params": {"action": "write", '
        '"path": "view.txt", "content": "VIEW"}}',
        '{"final_answer": "视图完成"}',
    )
    app = AlphaSWEApp("视图测试", config=cfg, llm=llm, planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(100):
            await pilot.pause(0.05)
            if app._finished is not None:
                break
        assert app._finished is not None and app._finished.ok

        # 主区默认主日志
        assert app.query_one("#main-log").display is True
        assert app.query_one("#diff-log").display is False
        # 任务面板应包含任务指令与阶段
        panel = app.query_one("#task-panel", Static)
        rendered = str(panel.render())
        assert "视图测试" in rendered and "阶段" in rendered
        # F5 轮换主区视图：日志 -> 文件变更 -> 监控
        await pilot.press("f5")
        assert app.query_one("#diff-log").display is True
        await pilot.press("f5")
        assert app.query_one("#metrics-view").display is True
        mon = app.query_one("#metrics-view", Static)
        assert "轮次" in str(mon.render())
        await pilot.press("f5")
        assert app.query_one("#main-log").display is True
        # F2 隐藏任务面板
        await pilot.press("f2")
        assert app.query_one("#task-panel").display is False
        await pilot.press("f2")
        assert app.query_one("#task-panel").display is True


# ---- 阶段八：确认弹窗 -> 批准所有同类操作 ----
@pytest.mark.asyncio
async def test_tui_confirmation_approve_all(ws_tmp):
    write = ('{"tool": "file_ops", "params": {"action": "write", '
             '"path": "a.txt", "content": "A"}}')
    write2 = ('{"tool": "file_ops", "params": {"action": "write", '
              '"path": "b.txt", "content": "B"}}')
    cfg = make_config(ws_tmp)
    cfg.agent.auto_approve = []  # 文件写入默认要求确认
    llm = ScriptedLLM(write, write2, '{"final_answer": "完成"}')
    app = AlphaSWEApp("确认测试", config=cfg, llm=llm, planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        # 等第一个确认请求弹窗
        for _ in range(100):
            await pilot.pause(0.05)
            if app._confirmations_asked >= 1:
                break
        assert app._confirmations_asked >= 1
        await pilot.pause(0.1)
        # 在弹窗输入 a + Enter：批准所有同类
        await pilot.press("a")
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.05)
            if app._finished is not None:
                break
        assert app._finished is not None and app._finished.ok
        assert app.runner is not None and app.runner.approve_rule == "file_write"
        # 两次写入都应成功（第二次免确认）
        assert (ws_tmp / "ws" / "a.txt").exists()
        assert (ws_tmp / "ws" / "b.txt").exists()


# ---- 阶段八：确认回调契约（loop 层） ----
@pytest.mark.asyncio
async def test_loop_approve_all_skips_subsequent(ws_tmp):
    """第一次确认返回 approved_all:file_write，后续同类调用免确认。"""
    calls = []

    async def cb(name, params, rule=None):
        calls.append((name, params, rule))
        return "approved_all:file_write"

    write = ('{"tool": "file_ops", "params": {"action": "write", '
             '"path": "x.txt", "content": "X"}}')
    write2 = ('{"tool": "file_ops", "params": {"action": "write", '
              '"path": "y.txt", "content": "Y"}}')
    cfg = make_config(ws_tmp)
    cfg.agent.auto_approve = []
    loop = AgentLoop(config=cfg, llm=ScriptedLLM(write, write2,
                                                 '{"final_answer": "ok"}'),
                     planner=StubPlanner(), confirmation_callback=cb)
    result = await loop.run("批准所有")
    assert result.ok
    assert len(calls) == 1  # 第二个 write 直接免确认
    assert "file_write" in loop._approve_rules
    assert (ws_tmp / "ws" / "x.txt").exists()
    assert (ws_tmp / "ws" / "y.txt").exists()
    names = [d["name"] for d in loop._decision.records()]
    assert "approve_all" in names


@pytest.mark.asyncio
async def test_loop_confirmation_modified_params(ws_tmp):
    """确认回调返回 dict -> 使用修改后的参数执行（阶段八 8.2）。"""

    async def cb(name, params, rule=None):
        return {"content": "MODIFIED", "path": "renamed.txt"}

    write = ('{"tool": "file_ops", "params": {"action": "write", '
             '"path": "orig.txt", "content": "ORIG"}}')
    cfg = make_config(ws_tmp)
    cfg.agent.auto_approve = []
    loop = AgentLoop(config=cfg, llm=ScriptedLLM(write, '{"final_answer": "ok"}'),
                     planner=StubPlanner(), confirmation_callback=cb)
    result = await loop.run("修改参数执行")
    assert result.ok
    assert (ws_tmp / "ws" / "renamed.txt").read_text(encoding="utf-8") == "MODIFIED"
    assert not (ws_tmp / "ws" / "orig.txt").exists()


# ---- 纯终端 UI 设计：窄屏布局 / 输入栏 / 历史 / 辅助渲染 ----
@pytest.mark.asyncio
async def test_tui_narrow_layout_auto_compact(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "窄屏完成"}')
    app = AlphaSWEApp("窄屏测试", config=cfg, llm=llm, planner=StubPlanner())
    async with app.run_test(size=(80, 40)) as pilot:
        for _ in range(100):
            await pilot.pause(0.05)
            if app._finished is not None:
                break
        # <100 列自动降级：任务面板隐藏，紧凑头显示
        assert app.query_one("#task-panel").display is False
        assert app.query_one("#compact-header").display is True
        # F4 切到宽屏
        await pilot.press("f4")
        assert app.query_one("#task-panel").display is True
        # F4 再按恢复自动（80 列仍为窄屏）
        await pilot.press("f4")
        assert app.query_one("#task-panel").display is False


@pytest.mark.asyncio
async def test_tui_input_submit_injects_instruction(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "注入完成"}')
    app = AlphaSWEApp("注入测试", config=cfg, llm=llm, planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(100):
            await pilot.pause(0.05)
            if app._finished is not None:
                break
        box = app.query_one("#input-bar", Input)
        box.focus()
        box.value = "改用 try-catch 方案"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        log = app.query_one("#main-log")
        rendered = "".join(str(line) for line in log.lines)
        assert "注入指令" in rendered
        assert "改用 try-catch 方案" in rendered


@pytest.mark.asyncio
async def test_tui_input_command_status(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "命令完成"}')
    app = AlphaSWEApp("命令测试", config=cfg, llm=llm, planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(100):
            await pilot.pause(0.05)
            if app._finished is not None:
                break
        box = app.query_one("#input-bar", Input)
        box.focus()
        box.value = "/status"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        log = app.query_one("#main-log")
        rendered = "".join(str(line) for line in log.lines)
        assert "状态:" in rendered and "阶段=" in rendered


@pytest.mark.asyncio
async def test_tui_input_history_navigation(ws_tmp):
    cfg = make_config(ws_tmp)
    app = AlphaSWEApp("历史测试", config=cfg, llm=MockLLM(),
                      planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._push_history("ls -la")
        app._push_history("git status")
        box = app.query_one("#input-bar", CommandInput)
        # 上箭头回到最后一条
        app.input_history_prev()
        assert box.value == "git status"
        app.input_history_prev()
        assert box.value == "ls -la"
        # 下箭头回到空
        app.input_history_next()
        assert box.value == "git status"
        app.input_history_next()
        assert box.value == ""


def test_layout_render_helpers():
    from agent.core.task import Task, TaskStatus
    from tui.app import _fmt_elapsed, _progress_bar, _task_row

    assert _progress_bar(50) == "[" + "=" * 10 + ">" + "-" * 9 + "]"
    assert _progress_bar(100) == "[" + "=" * 20 + "]"
    assert _fmt_elapsed(207) == "00:03:27"
    t = Task(id="s::step", instruction="定位空指针",
             status=TaskStatus.RUNNING,
             metadata={"skill": "s", "skill_step": "step",
                       "step_index": 0, "step_total": 3})
    row = _task_row(t)
    assert "进行>" in row and "step 1/3" in row



def test_logbridge_posts_log_message():
    """日志桥把 logging 记录转成 LogMessage，绝不向 stdout 打印。"""
    import logging
    from tui.logbridge import TuiLogHandler
    from tui.messages import LogMessage

    captured = []

    class FakeApp:
        def post_message(self, msg):
            captured.append(msg)

    handler = TuiLogHandler()
    handler.set_app(FakeApp())
    record = logging.LogRecord(
        "alpha-swe.mcp", logging.WARNING, "", 0,
        "MCP 服务器 custom-knowledge 连接失败", None, None,
    )
    handler.emit(record)
    assert len(captured) == 1
    msg = captured[0]
    assert isinstance(msg, LogMessage)
    assert msg.level == "WARNING"
    assert "alpha-swe.mcp" in msg.content


def test_logbridge_install_removes_stdout_handlers(ws_tmp):
    """install_tui_logging 应移除既有 stdout handler 并挂上文件/桥接 handler。

    只验证调用时刻的清理效果：pytest 自身的日志插件会在用例期间动态重新挂载，
    因此不要求调用后整个 root.handlers 里完全没有 StreamHandler。
    """
    import logging
    from tui.logbridge import TuiLogHandler, install_tui_logging

    root = logging.getLogger()
    before_all = list(root.handlers)
    before_stream = [h for h in before_all
                     if isinstance(h, logging.StreamHandler)]
    before_level = root.level
    try:
        bridge = install_tui_logging(verbose=False,
                                     log_file=str(ws_tmp / "tui.log"))
        assert isinstance(bridge, TuiLogHandler)
        for h in before_stream:
            assert h not in root.handlers, f"stdout handler 未移除: {h}"
        assert any(isinstance(h, logging.FileHandler) for h in root.handlers)
        assert bridge in root.handlers
        assert (ws_tmp / "tui.log").exists()
    finally:
        root.handlers = before_all
        root.setLevel(before_level)
