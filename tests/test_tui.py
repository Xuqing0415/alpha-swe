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
    from textual.containers import Vertical

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

    from tui.timeline_view import summarize

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

