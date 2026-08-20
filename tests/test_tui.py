"""TUI 测试：事件格式化、事件订阅、终端实时输出桥接、Textual 无头运行。"""
import json
import re
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
from tui.app import (AlphaSWEApp, CommandInput, RegressionScreen)
from tui.messages import AgentEventMessage
from textual.widgets import Input, Static


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


async def _query_input_with_retry(app, pilot, widget_type):
    """负载下 #input-bar 查询偶发 NoMatches，带重试提升稳定性。"""
    for _ in range(30):
        try:
            return app.query_one("#input-bar", widget_type)
        except Exception:
            await pilot.pause(0.05)
    return app.query_one("#input-bar", widget_type)


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class BgScriptedLLM(MockLLM):
    """后台任务联动：首轮启动，次轮解析 task_id 查状态，最后收尾。"""

    def __init__(self, command):
        super().__init__()
        self.command = command
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        n = len(self.calls)
        if n == 1:
            return json.dumps({
                "tool": "background_task",
                "params": {"action": "start", "command": self.command},
            }, ensure_ascii=False)
        if n == 2:
            blob = "".join(str(m.get("content", "")) for m in messages)
            match = re.search(r"后台任务已启动: ([0-9a-f]{8})", blob)
            assert match, f"未在上下文中解析到 task_id: {blob[:400]}"
            return json.dumps({
                "tool": "background_task",
                "params": {"action": "status", "task_id": match.group(1)},
            }, ensure_ascii=False)
        return '{"final_answer": "后台任务联动完成"}'


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
        # F5 轮换主区视图：日志 -> 文件变更 -> 监控 -> 时间线 -> 日志
        await pilot.press("f5")
        assert app.query_one("#diff-log").display is True
        await pilot.press("f5")
        assert app.query_one("#metrics-view").display is True
        mon = app.query_one("#metrics-view", Static)
        assert "轮次" in str(mon.render())
        await pilot.press("f5")
        assert app.query_one("#timeline-view").display is True
        app.refresh_views()
        assert "总耗时" in str(app.query_one("#timeline-view").content)
        # F5 轮换含后台视图：时间线 -> 后台 -> 日志
        await pilot.press("f5")
        assert app.query_one("#bg-view").display is True
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
        box = await _query_input_with_retry(app, pilot, Input)
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



def test_task_row_retry_indicator():
    """方案 1.1/2.3：重试中的任务显示 (重试 x/y)。"""
    from agent.core.task import Task, TaskStatus
    from tui.app import _task_row

    t = Task(id="t", instruction="重试任务", status=TaskStatus.RETRYING,
             retry_count=2, max_retries=3, criticality="critical")
    row = _task_row(t)
    assert "重试 " in row and "(重试 2/3)" in row
    # 未重试过的任务不显示
    t2 = Task(id="t2", instruction="普通任务", status=TaskStatus.RUNNING)
    assert "(重试" not in _task_row(t2)


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


# ---- 虚拟滚动主日志区（DataTable 三列） ----
@pytest.mark.asyncio
async def test_virtual_log_write_lines_and_follow(ws_tmp):
    """VirtualLog 三列写入、lines 兼容、跟随/浏览模式切换。"""
    from rich.text import Text
    from textual.app import App

    from tui.vlog import VirtualLog

    class LogApp(App[None]):
        def compose(self):
            yield VirtualLog(id="log", max_lines=50)

    async with LogApp().run_test(size=(100, 20)) as pilot:
        vlog = pilot.app.query_one("#log", VirtualLog)
        vlog.write(Text("[12:00:01] THINK 分析任务", style=""))
        vlog.write(Text("[12:00:02] ACT terminal: ls", style=""))
        vlog.write(Text("已暂停", style="yellow"))
        assert vlog.row_count == 3
        rendered = "".join(str(line) for line in vlog.lines)
        assert "THINK" in rendered and "分析任务" in rendered
        assert vlog.lines[-1].plain == "已暂停"
        # 跟随 -> 浏览 -> 恢复
        assert vlog.follow is True
        vlog.watch_scroll_y(30, 10)
        assert vlog.follow is False
        vlog.watch_scroll_y(10, 30)
        assert vlog.follow is True


@pytest.mark.asyncio
async def test_virtual_log_ring_buffer_prunes(ws_tmp):
    """环形缓冲：超过 max_lines 批量裁剪最旧行。"""
    from rich.text import Text
    from textual.app import App

    from tui.vlog import VirtualLog

    class LogApp(App[None]):
        def compose(self):
            yield VirtualLog(id="log", max_lines=50)

    async with LogApp().run_test(size=(100, 20)) as pilot:
        vlog = pilot.app.query_one("#log", VirtualLog)
        for i in range(90):
            vlog.write(Text(f"[00:00:{i % 60:02d}] INFO 第 {i} 行", style=""))
        # 批量裁剪：行数保持在 max_lines 与 max_lines+阈值之间
        assert 50 <= vlog.row_count <= 75, vlog.row_count
        rendered = "".join(str(line) for line in vlog.lines)
        assert "第 0 行" not in rendered  # 最旧行已被裁剪
        assert "第 40 行" in rendered
        assert "第 89 行" in rendered


@pytest.mark.asyncio
async def test_tui_status_bar_shows_follow_hint(ws_tmp):
    """状态栏在日志视图显示 [跟随]/[浏览中] 提示。"""
    from textual.widgets import Static

    from tui.vlog import VirtualLog

    cfg = make_config(ws_tmp)
    app = AlphaSWEApp("跟随提示测试", config=cfg, llm=MockLLM(),
                      planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        vlog = app.query_one("#main-log", VirtualLog)
        vlog.watch_scroll_y(50, 10)  # 模拟上滚 -> 浏览中
        app.refresh_status()
        bar = app.query_one("#status-bar", Static)
        assert "[浏览中]" in str(bar.content)
        vlog.watch_scroll_y(10, 60)  # 到底 -> 恢复跟随
        app.refresh_status()
        assert "[跟随]" in str(bar.content)


# ---- Diff 视图：unified diff 渲染与 D 键切换 ----
def test_diff_renderer_unified_diff_lines():
    """render_unified_diff 输出标准 unified diff 并着色分类。"""
    from tui.diff_renderer import render_unified_diff

    before = "def f():\n    return 1\n"
    after = "def f():\n    return 2\n"
    lines = render_unified_diff("src/a.py", before, after)
    plain = [line.plain for line in lines]
    assert plain[0].startswith("--- a/src/a.py")
    assert plain[1].startswith("+++ b/src/a.py")
    assert any(line.startswith("@@") for line in plain)
    assert "-    return 1" in plain
    assert "+    return 2" in plain
    # 新建文件：全部 + 行
    new_lines = render_unified_diff("src/new.py", None, "x=1\n")
    new_plain = [line.plain for line in new_lines]
    assert new_plain[1].startswith("+++ b/src/new.py")
    assert any(line.startswith("+x=1") for line in new_plain)


@pytest.mark.asyncio
async def test_tui_diff_event_and_d_toggle(ws_tmp):
    """tool_call 携带 meta 时渲染 unified diff，D 键在 diff/输出间切换。"""
    from textual.widgets import RichLog

    cfg = make_config(ws_tmp)
    app = AlphaSWEApp("diff 测试", config=cfg, llm=MockLLM(),
                      planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        record = {
            "type": "tool_call",
            "data": {
                "tool": "file_ops",
                "params": {"action": "write", "path": "src/app.py"},
                "success": True,
                "meta": {"path": "src/app.py", "diff_before": "old",
                         "diff_after": "new"},
            },
        }
        # 先切到 diff 主视图再触发事件：隐藏的 #diff-log 宽度为 0 不会渲染行
        app.action_cycle_main_view()  # log -> diff
        await pilot.pause()
        app.on_agent_event_message(type("E", (), {"record": record})())
        await pilot.pause()
        assert app._diff_path == "src/app.py"
        assert len(app._diff_lines) >= 3
        diff_log = app.query_one("#diff-log", RichLog)
        rendered = "".join(str(line) for line in diff_log.lines)
        assert "+new" in rendered and "-old" in rendered
        # D 键：终端区切到 diff
        assert app._terminal_diff_mode is False
        app.action_toggle_terminal_diff()
        await pilot.pause()
        assert app._terminal_diff_mode is True
        term_log = app.query_one("#terminal-log", RichLog)
        term_rendered = "".join(str(line) for line in term_log.lines)
        assert "+new" in term_rendered
        # 再按 D 恢复原始输出
        app.action_toggle_terminal_diff()
        await pilot.pause()
        assert app._terminal_diff_mode is False


# ---- 文件树视图：构建/渲染 + F6 切换 + 搜索 + 标记 ----
def test_file_tree_build_render_collapse_filter(ws_tmp):
    """build_tree / iter_visible / render_node：目录优先、忽略、折叠、过滤。"""
    from tui.file_tree import build_tree, iter_visible, render_node

    ws = ws_tmp / "ft"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("x", encoding="utf-8")
    (ws / "src" / "b.ts").write_text("yy", encoding="utf-8")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "pkg").write_text("z", encoding="utf-8")
    (ws / "README.md").write_text("hi", encoding="utf-8")

    tree = build_tree(str(ws))
    assert tree is not None
    rows = list(iter_visible(tree))
    names = [r[0].name for r in rows]
    assert "node_modules" not in names  # 默认忽略
    assert names.index("src") < names.index("README.md")  # 目录在前
    # 折叠
    collapsed_rows = list(iter_visible(tree, collapsed={str(ws / "src")}))
    assert "a.py" not in [r[0].name for r in collapsed_rows]
    # 过滤
    filtered = list(iter_visible(tree, filter_text="a.py"))
    assert "a.py" in [r[0].name for r in filtered]
    # 渲染标记：* 标记修改过的路径
    row = render_node(tree, 0, True, modified={str(ws)})
    assert "*" in row.plain
    txt = render_node(tree.children[0], 1, False, active=str(ws / "src"))
    assert ">" in txt.plain and "|-- " in txt.plain


@pytest.mark.asyncio
async def test_tui_file_tree_toggle_and_search(ws_tmp):
    """F6 切换任务面板/文件树；/ 进入搜索实时过滤；Esc 恢复。"""
    from textual.containers import Vertical
    from textual.widgets import Input, Static

    from tui.file_tree import FileTreeView

    ws = ws_tmp / "ws"
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "src" / "utils.py").write_text("y = 2\n", encoding="utf-8")

    cfg = make_config(ws_tmp)
    app = AlphaSWEApp("文件树测试", config=cfg, llm=MockLLM(),
                      planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app._left_view == "tasks"
        app.action_toggle_file_tree()
        await pilot.pause()
        assert app._left_view == "tree"
        assert app.query_one("#file-tree-box", Vertical).display is True
        assert app.query_one("#task-panel", Static).display is False
        tree = app.query_one("#file-tree", FileTreeView)
        assert tree._tree is not None
        # 搜索：/ 进入过滤模式
        tree.action_focus_search()
        await pilot.pause()
        assert app._tree_filter_mode is True
        box = app.query_one("#input-bar", Input)
        box.value = "utils"
        app.on_input_changed(type("C", (), {"value": "utils",
                                            "input": box})())
        await pilot.pause()
        texts = [str(item.children[0].content) for item in tree.children]
        rendered = "".join(texts)
        assert "utils.py" in rendered and "main.py" not in rendered
        # Esc 恢复完整树
        app._maybe_exit_tree_filter()
        await pilot.pause()
        assert app._tree_filter_mode is False
        rendered2 = "".join(str(item.children[0].content)
                             for item in tree.children)
        assert "main.py" in rendered2


@pytest.mark.asyncio
async def test_tui_file_tree_marks_from_tool_call(ws_tmp):
    """tool_call 事件更新文件树修改/活动标记。"""
    from tui.file_tree import FileTreeView

    ws = ws_tmp / "ws"
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "main.py").write_text("x\n", encoding="utf-8")

    cfg = make_config(ws_tmp)
    app = AlphaSWEApp("文件树标记", config=cfg, llm=MockLLM(),
                      planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        record = {
            "type": "tool_call",
            "data": {"tool": "file_ops", "params": {"action": "write",
                     "path": "src/main.py"},
                    "success": True,
                    "meta": {"path": "src/main.py"}},
        }
        app.on_agent_event_message(type("E", (), {"record": record})())
        await pilot.pause()
        assert "src/main.py" in app._tree_modified
        assert app._tree_active == "src/main.py"
        app.action_toggle_file_tree()
        await pilot.pause()
        tree = app.query_one("#file-tree", FileTreeView)
        rendered = "".join(str(item.children[0].content)
                             for item in tree.children)
        assert "main.py" in rendered


# ---- 火焰图/时间线视图 ----
def test_timeline_render_horizontal_and_waterfall():
    """render_timeline 输出 # 条、时间轴与汇总；窄屏切瀑布。"""
    from tui.timeline_view import (render_horizontal, render_timeline,
                                   render_waterfall, summarize)

    rows = [
        {"name": "think: 分析", "kind": "llm", "start": 0.0,
         "duration": 1.2, "status": "ok"},
        {"name": "tool: terminal: ls", "kind": "tool", "start": 1.2,
         "duration": 0.3, "status": "ok"},
        {"name": "task: 修复", "kind": "task", "start": 1.5,
         "duration": 2.1, "status": "error"},
    ]
    lines = render_horizontal(rows, axis_width=40)
    plain = [line.plain for line in lines]
    assert "3.6s" in plain[0]  # 时间轴末端刻度
    assert any("#" in line for line in plain)
    assert plain[-1].endswith("最慢: task: 修复 (2.1s)")
    waterfall = render_waterfall(rows, bar_width=30)
    assert any("[" in line.plain for line in waterfall)
    assert summarize(rows).startswith("总耗时: 3.6s")
    narrow = render_timeline(rows, width=80, narrow=True)
    assert any("[" in line.plain for line in narrow)


@pytest.mark.asyncio
async def test_tui_f5_cycles_to_timeline_view(ws_tmp):
    """F5 循环到时间线视图并渲染 tracer span。"""
    from textual.widgets import Static


    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "完成"}')
    app = AlphaSWEApp("时间线测试", config=cfg, llm=llm,
                      planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(100):
            await pilot.pause(0.05)
            if app._finished is not None:
                break
        # 注入两条 span 再切视图
        loop = app.runner.loop
        s1 = loop.tracer.start_span("task:t0", "task")
        s1.end("ok")
        # 循环到 timeline
        for _ in range(len(__import__("tui.app", fromlist=["_MAIN_VIEWS"])._MAIN_VIEWS)):
            app.action_cycle_main_view()
            await pilot.pause()
            if app._main_view == "timeline":
                break
        assert app._main_view == "timeline"
        app.refresh_views()
        view = app.query_one("#timeline-view", Static)
        text = str(view.content)
        assert "task:t0" in text or "总耗时" in text



# ---- 可选项交互：时间线选中/详情 + 文件树增量更新 ----
@pytest.mark.asyncio
async def test_tui_timeline_select_and_detail(ws_tmp):
    """时间线视图：上/下选中 span，Enter 弹出详情（参数/输出/错误）。"""
    from tui.timeline_view import TimelineView

    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "完成"}')
    app = AlphaSWEApp("时间线交互", config=cfg, llm=llm,
                      planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(100):
            await pilot.pause(0.05)
            if app._finished is not None:
                break
        loop = app.runner.loop
        s1 = loop.tracer.start_span("tool:terminal: ls", "tool",
                                    params='{"command": "ls"}')
        s1.end("ok", out="src/ tests/")
        s2 = loop.tracer.start_span("task:t0", "task")
        s2.end("error", "boom")
        # 循环到时间线视图
        for _ in range(len(__import__("tui.app",
                                      fromlist=["_MAIN_VIEWS"])._MAIN_VIEWS)):
            app.action_cycle_main_view()
            await pilot.pause()
            if app._main_view == "timeline":
                break
        app.refresh_views()
        view = app.query_one("#timeline-view", TimelineView)
        assert len(view._rows) >= 2
        assert view._selected == 0
        view.focus()  # 时间线视图接收键盘
        await pilot.pause()
        # 选中行应带 > 标记
        assert ">" in str(view.content)
        # 定位到工具 span（含 params/out），再上下移动验证选中
        idx = next(i for i, r in enumerate(view._rows)
                   if r["name"] == "tool:terminal: ls")
        for _ in range(idx):
            await pilot.press("down")
        assert view._selected == idx
        await pilot.press("up")
        assert view._selected == idx - 1
        await pilot.press("down")
        assert view._selected == idx
        # Enter 打开详情弹窗（轮询等挂载，弹窗组件挂在 app.screen）
        await pilot.press("enter")
        box = None
        for _ in range(50):
            await pilot.pause(0.05)
            try:
                box = app.screen.query_one("#timeline-detail-box", Static)
                break
            except Exception:
                continue
        assert box is not None, "详情弹窗未挂载"
        text = str(box.render())
        assert "tool:terminal: ls" in text
        assert "参数" in text and "command" in text
        assert "输出" in text and "src/ tests/" in text
        # Esc 关闭
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ != "TimelineDetailScreen"


@pytest.mark.asyncio
async def test_tui_file_tree_incremental_new_file(ws_tmp):
    """文件树增量更新：Agent 新建文件后自动出现在树中并打 * 标记。"""
    from tui.file_tree import FileTreeView

    ws = ws_tmp / "ws"
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")

    cfg = make_config(ws_tmp)
    app = AlphaSWEApp("文件树增量", config=cfg, llm=MockLLM(),
                      planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_toggle_file_tree()  # 先构建一次
        await pilot.pause()
        tree = app.query_one("#file-tree", FileTreeView)
        texts = "".join(str(item.children[0].content)
                         for item in tree.children)
        assert "main.py" in texts and "new.py" not in texts
        # Agent 新建文件（真实事件，meta 为绝对路径）
        new_file = ws / "src" / "new.py"
        new_file.write_text("y = 2\n", encoding="utf-8")
        record = {
            "type": "tool_call",
            "data": {"tool": "file_ops",
                     "params": {"action": "write", "path": "src/new.py"},
                     "success": True,
                     "meta": {"path": str(new_file)}},
        }
        app.on_agent_event_message(type("E", (), {"record": record})())
        await pilot.pause()
        texts = "".join(str(item.children[0].content)
                         for item in tree.children)
        assert "new.py" in texts, texts
        # 新文件应带 * 修改标记（绝对路径与树节点一致）
        assert any("*" in str(item.children[0].content)
                   for item in tree.children)

# ---- 文件树多选/批量操作（本迭代） ----
def test_file_tree_render_selected_marker(ws_tmp):
    """render_node：selected 路径渲染 [x] 标记（多选视觉反馈）。"""
    from tui.file_tree import build_tree, render_node

    ws = ws_tmp / "ft"
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "a.py").write_text("x", encoding="utf-8")
    tree = build_tree(str(ws))
    assert tree is not None
    node = tree.children[0].children[0]  # src/a.py
    plain = render_node(node, 2, True, selected={node.path}).plain
    assert "[x]" in plain
    plain2 = render_node(node, 2, True, selected=set()).plain
    assert "[x]" not in plain2


@pytest.mark.asyncio
async def test_tui_file_tree_multi_select_actions(ws_tmp):
    """文件树多选：Space 连选 + [x] 标记 + 标题计数 + a/x/c/o 批量操作。"""
    from tui.file_tree import FileTreeView

    ws = ws_tmp / "ws"
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "src" / "utils.py").write_text("y = 2\n", encoding="utf-8")

    cfg = make_config(ws_tmp)
    app = AlphaSWEApp("文件树多选", config=cfg, llm=MockLLM(),
                      planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_toggle_file_tree()
        await pilot.pause()
        tree = app.query_one("#file-tree", FileTreeView)
        assert tree.has_focus, "进入文件树后应获得焦点"
        # 导航到 main.py 并按 Space 选中，再下移选中 utils.py
        await pilot.press("down", "down")
        await pilot.press("space")
        await pilot.press("down")
        await pilot.press("space")
        await pilot.pause()
        assert tree.selection_count() == 2
        rendered = "".join(str(item.children[0].content)
                           for item in tree.children)
        assert rendered.count("[x]") == 2, rendered
        # 标题显示已选计数
        title = app.query_one("#file-tree-title")
        assert "已选 2" in str(title.render())
        # x 清空选择
        await pilot.press("x")
        await pilot.pause()
        assert tree.selection_count() == 0
        # a 全选当前可见文件
        await pilot.press("a")
        await pilot.pause()
        assert tree.selection_count() >= 2
        # c 复制路径到输入栏（供继续输入命令）
        await pilot.press("c")
        await pilot.pause()
        box = app.query_one("#input-bar", Input)
        assert "main.py" in box.value and "utils.py" in box.value
        # o 批量打开：终端区预览各文件内容
        # （c 已把焦点切到输入栏，先切回文件树再按 o）
        tree.focus()
        await pilot.pause()
        before = len(app._terminal_lines)
        await pilot.press("o")
        await pilot.pause()
        joined = "".join(str(t) for t in app._terminal_lines[before:])
        assert "批量打开" in joined
        assert "x = 1" in joined and "y = 2" in joined


# ---- 方案 2.4：F5 后台视图与 /bg 命令联动真实后台任务 ----
@pytest.mark.asyncio
async def test_tui_bg_view_and_bg_command(ws_tmp):
    cfg = make_config(ws_tmp)
    command = (
        "python -c \"import time; "
        "print('bg-up', flush=True); time.sleep(30)\""
    )
    llm = BgScriptedLLM(command)
    app = AlphaSWEApp("后台联动测试", config=cfg, llm=llm,
                      planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(200):
            await pilot.pause(0.05)
            if app._finished is not None:
                break
        assert app._finished is not None and app._finished.ok
        # 事件缓存应记录后台任务（manager 已被 close 清空，缓存保留快照）
        assert app._bg_tasks, "后台任务事件缓存不应为空"
        tid = next(iter(app._bg_tasks))
        # F5 切到后台视图（日志 -> 变更 -> 监控 -> 时间线 -> 后台）
        for _ in range(5):
            await pilot.press("f5")
            if app.query_one("#bg-view", Static).display:
                break
        assert app.query_one("#bg-view", Static).display is True
        app.refresh_views()  # 与时间线视图一致：切换后主动刷新内容
        rendered = str(app.query_one("#bg-view", Static).render())
        assert "后台任务" in rendered and tid in rendered
        # /bg 命令列出后台任务
        box = await _query_input_with_retry(app, pilot, Input)
        box.focus()
        box.value = "/bg"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        log = app.query_one("#main-log")
        joined = "".join(str(line) for line in log.lines)
        assert "后台" in joined and tid in joined

# ---- 进阶 3.3：回归检测 TUI 联动（格式渲染 + 统计 + 终端区高亮） ----
def test_format_event_regression_events():
    clean = format_event({"type": "regression_clean",
                          "data": {"module": "app.py",
                                   "test_file": "test_app.py"}})
    assert "OK" in clean.plain and "回归检测通过" in clean.plain
    assert "app.py" in clean.plain and "test_app.py" in clean.plain
    det = format_event({"type": "regression_detected",
                        "data": {"module": "app.py",
                                 "test_file": "test_app.py",
                                 "summary": "assert 1 == 2 failed"}})
    assert "REG" in det.plain and "回归检测失败" in det.plain
    assert "assert 1 == 2 failed" in det.plain
    skip = format_event({"type": "regression_skip",
                         "data": {"module": "app.py",
                                  "test_file": "test_app.py"}})
    assert "回归检测跳过" in skip.plain and "app.py" in skip.plain


@pytest.mark.asyncio
async def test_tui_regression_event_linkage(ws_tmp):
    """回归事件驱动 TUI：统计累加、失败摘要进终端区、/status 汇总。"""
    cfg = make_config(ws_tmp)
    app = AlphaSWEApp("回归联动测试", config=cfg, planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        app.post_message(AgentEventMessage({
            "type": "regression_clean",
            "data": {"module": "app.py", "test_file": "test_app.py"},
        }))
        app.post_message(AgentEventMessage({
            "type": "regression_detected",
            "data": {"module": "app.py", "test_file": "test_app.py",
                     "summary": "assert 1 == 2 failed"},
        }))
        app.post_message(AgentEventMessage({
            "type": "regression_skip",
            "data": {"module": "app.py", "test_file": "test_app.py"},
        }))
        await pilot.pause()
        await pilot.pause()
        assert app._regression_stats == {"clean": 1, "detected": 1, "skip": 1}
        # 检测失败时失败摘要高亮写入终端输出区
        joined = "".join(str(t) for t in app._terminal_lines)
        assert "回归检测失败" in joined
        assert "assert 1 == 2 failed" in joined
        # /status 输出回归汇总
        box = await _query_input_with_retry(app, pilot, Input)
        box.focus()
        box.value = "/status"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        log = app.query_one("#main-log")
        rendered = "".join(str(line) for line in log.lines)
        assert "回归" in rendered
        assert "通过1" in rendered and "失败1" in rendered and "跳过1" in rendered


# ---- 进阶 3.3 联动补强：回归失败全屏摘要视图（F7 / /reg） ----
def test_tui_regression_failure_tracking_bounded():
    """回归失败明细按记录追加，缓存上限 200 条。"""
    app = AlphaSWEApp("回归明细缓存", planner=StubPlanner())
    app._append_terminal = lambda renderable: None  # 未挂载时屏蔽终端区渲染
    for i in range(250):
        app._track_regression({
            "type": "regression_detected",
            "data": {"module": "m%d.py" % i,
                     "test_file": "test_m%d.py" % i,
                     "summary": "assert %d == %d failed" % (i, i + 1)},
        })
    assert len(app._regression_failures) == 200, "失败明细应裁剪到 200 条"
    first = app._regression_failures[0]
    assert first["module"] == "m50.py"
    last = app._regression_failures[-1]
    assert last["module"] == "m249.py"
    assert "assert 249 == 250 failed" in last["summary"]


@pytest.mark.asyncio
async def test_tui_regression_fullscreen_summary(ws_tmp):
    """F7 全屏摘要：统计头 + 逐条失败项，Esc 关闭。"""
    cfg = make_config(ws_tmp)
    app = AlphaSWEApp("回归全屏测试", config=cfg, planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        app.post_message(AgentEventMessage({
            "type": "regression_clean",
            "data": {"module": "app.py", "test_file": "test_app.py"},
        }))
        app.post_message(AgentEventMessage({
            "type": "regression_detected",
            "data": {"module": "app.py", "test_file": "test_app.py",
                     "summary": "assert 1 == 2 failed\nat test_app.py:42"},
        }))
        app.post_message(AgentEventMessage({
            "type": "regression_clean",
            "data": {"module": "auth.py", "test_file": "test_auth.py"},
        }))
        app.post_message(AgentEventMessage({
            "type": "regression_skip",
            "data": {"module": "x.py", "test_file": "test_x.py"},
        }))
        await pilot.pause()
        await pilot.pause()
        assert app._regression_stats == {"clean": 2, "detected": 1, "skip": 1}
        assert len(app._regression_failures) == 1

        await pilot.press("f7")
        await pilot.pause()
        assert isinstance(app.screen, RegressionScreen), "F7 应打开全屏视图"
        reg = app.screen
        log = reg.query_one("#reg-full-log")
        joined = "".join(getattr(line, "text", "") or str(line)
                         for line in log.lines)
        assert "回归统计" in joined
        assert "通过 2" in joined and "失败 1" in joined and "跳过 1" in joined
        assert "app.py" in joined and "test_app.py" in joined
        assert "assert 1 == 2 failed" in joined
        assert "test_app.py:42" in joined

        # Esc 关闭回到主界面
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RegressionScreen)


@pytest.mark.asyncio
async def test_tui_error_screen_shown_on_runner_failure(ws_tmp, monkeypatch):
    """runner 抛异常时：错误落盘、任务结束、弹出错误面板，Esc 关闭。"""
    from tui.app import ErrorScreen

    captured = {}

    def fake_write_error_log(exc, *, context=None, session_id=""):
        captured["exc"] = exc
        captured["context"] = context
        return str(ws_tmp / "logs" / "cli_error_test.log")

    monkeypatch.setattr("tui.app.write_error_log", fake_write_error_log)

    class BoomRunner:
        loop = None
        result = None

        async def run(self):
            raise RuntimeError("boom-task")

        def total_rounds(self):
            return 0

        def dag_summary(self):
            return {"by_status": {}}

    class BoomApp(AlphaSWEApp):
        def _make_runner(self):
            return BoomRunner()

    cfg = make_config(ws_tmp)
    app = BoomApp("故障注入测试", config=cfg, planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(120):
            await pilot.pause(0.05)
            if isinstance(pilot.app.screen, ErrorScreen):
                break
        assert isinstance(pilot.app.screen, ErrorScreen), (
            f"应弹出错误面板, screen={type(pilot.app.screen)}")
        assert isinstance(app._finished, RuntimeError)
        assert captured["context"]["module"] == "tui.run_agent"
        screen = pilot.app.screen
        log = screen.query_one("#error-full-log")
        joined = "".join(getattr(line, "text", "") or str(line)
                         for line in log.lines)
        assert "boom-task" in joined
        assert "\u5b8c\u6574\u9519\u8bef\u65e5\u5fd7" in joined
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, ErrorScreen)
@pytest.mark.asyncio
async def test_tui_event_status_refresh_throttled(ws_tmp, monkeypatch):
    """排查方案 3.2：高频事件下事件驱动的状态刷新被节流（< 事件数）。"""
    cfg = make_config(ws_tmp)
    calls: list = []
    orig_refresh = AlphaSWEApp.refresh_status

    def counting(self):
        calls.append(1)
        return orig_refresh(self)

    monkeypatch.setattr(AlphaSWEApp, "refresh_status", counting)
    app = AlphaSWEApp("节流测试", config=cfg, llm=MockLLM(),
                      planner=StubPlanner())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for _ in range(40):
            app.post_message(AgentEventMessage(
                {"type": "think", "data": {"content": "x"}}))
        await pilot.pause(0.4)
        # 40 个事件在节流窗口内不应触发 40 次全量刷新
        assert len(calls) < 40, f"事件驱动刷新未被节流: {len(calls)} 次"
        # 显式调用 refresh_status 不受节流影响（直调仍需立即生效）
        app.refresh_status()
        assert len(calls) > 0
