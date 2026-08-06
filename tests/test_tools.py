"""异步工具测试。"""
import pytest

from agent.tools.base import ExecutionContext
from agent.tools.fileio import FileIOTool, resolve_workspace_path
from agent.tools.terminal import TerminalTool


@pytest.mark.asyncio
async def test_fileio_write_read_search(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = FileIOTool()

    r = await tool.execute({"action": "write", "path": "src/app.py",
                            "content": "def main():\n    print('hello')\n"}, ctx)
    assert r.success

    r = await tool.execute({"action": "read", "path": "src/app.py"}, ctx)
    assert r.success and "def main()" in r.output

    r = await tool.execute({"action": "search", "path": "src",
                            "pattern": "print"}, ctx)
    assert r.success and r.metadata["hits"] >= 1


@pytest.mark.asyncio
async def test_fileio_blocks_traversal(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = FileIOTool()
    r = await tool.execute({"action": "read", "path": "../outside.txt"}, ctx)
    assert r.success is False
    assert "穿越" in r.error or "越界" in r.error


def test_resolve_workspace_path_rejects_escape(ws_tmp):
    with pytest.raises(PermissionError):
        resolve_workspace_path(str(ws_tmp), "../evil.txt")
    # 工作区内绝对路径合法
    ok = resolve_workspace_path(str(ws_tmp), str(ws_tmp / "ok.txt"))
    assert ok.name == "ok.txt"


@pytest.mark.asyncio
async def test_terminal_echo(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = TerminalTool()
    r = await tool.execute({"command": "echo hello-swe", "timeout": 10}, ctx)
    assert r.success
    assert "hello-swe" in r.output


@pytest.mark.asyncio
async def test_terminal_timeout(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = TerminalTool()
    r = await tool.execute({"command": "Start-Sleep -Seconds 5", "timeout": 1}, ctx)
    assert r.success is False
    assert r.metadata.get("timed_out") is True