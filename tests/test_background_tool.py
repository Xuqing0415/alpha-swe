"""后台任务工具测试（方案 2.4）：启动/状态/日志/优雅关闭/崩溃检测。"""
import asyncio

import pytest

from agent.tools.base import ExecutionContext
from agent.tools.background import BackgroundTaskTool


def make_ctx(ws_tmp):
    (ws_tmp / "ws").mkdir(parents=True, exist_ok=True)
    return ExecutionContext(workspace=str(ws_tmp / "ws"))


@pytest.mark.asyncio
async def test_background_start_status_logs_stop(ws_tmp):
    """启动长驻进程 -> 查询运行中 -> 读日志 -> 优雅关闭。"""
    tool = BackgroundTaskTool()
    ctx = make_ctx(ws_tmp)
    r = await tool.execute(
        {"action": "start",
         "command": "python -c \"import time; print('bg-up', flush=True); time.sleep(30)\""},
        ctx,
    )
    assert r.success
    task_id = r.metadata["task_id"]
    assert task_id

    await asyncio.sleep(0.8)
    r = await tool.execute({"action": "status", "task_id": task_id}, ctx)
    assert r.success
    assert "running" in r.output

    r = await tool.execute({"action": "logs", "task_id": task_id, "lines": 10}, ctx)
    assert r.success
    assert "bg-up" in r.output

    r = await tool.execute({"action": "stop", "task_id": task_id, "graceful": True}, ctx)
    assert r.success
    r = await tool.execute({"action": "status", "task_id": task_id}, ctx)
    assert r.success
    assert "stopped" in r.output


@pytest.mark.asyncio
async def test_background_unknown_task_errors(ws_tmp):
    tool = BackgroundTaskTool()
    ctx = make_ctx(ws_tmp)
    r = await tool.execute({"action": "status", "task_id": "nope"}, ctx)
    assert r.success is False
    r = await tool.execute({"action": "logs", "task_id": "nope"}, ctx)
    assert r.success is False


@pytest.mark.asyncio
async def test_background_crash_detection(ws_tmp):
    """进程意外退出应记录为 crashed 并给出退出码与最后输出。"""
    tool = BackgroundTaskTool()
    ctx = make_ctx(ws_tmp)
    r = await tool.execute(
        {"action": "start",
         "command": "python -c \"import sys; print('about-to-fail', flush=True); sys.exit(3)\""},
        ctx,
    )
    assert r.success
    task_id = r.metadata["task_id"]
    # 等待子进程退出并被监视任务捕获
    for _ in range(50):
        await asyncio.sleep(0.1)
        r = await tool.execute({"action": "status", "task_id": task_id}, ctx)
        if "crashed" in r.output:
            break
    assert "crashed" in r.output
    assert "exit_code=3" in r.output or "exit_code = 3" in r.output
    assert "about-to-fail" in r.output


@pytest.mark.asyncio
async def test_background_requires_command(ws_tmp):
    tool = BackgroundTaskTool()
    ctx = make_ctx(ws_tmp)
    r = await tool.execute({"action": "start", "command": "  "}, ctx)
    assert r.success is False