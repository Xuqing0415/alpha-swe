"""精确行编辑测试（方案 3.2）：file_ops edit 只改动目标行。"""
import pytest

from agent.tools.base import ExecutionContext
from agent.tools.fileio import FileIOTool

ORIG = "line1\nline2\nline3\nline4\nline5\n"


def make_tool(read_only=False):
    return FileIOTool(read_only=read_only)


@pytest.mark.asyncio
async def test_file_edit_replaces_line_range(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = make_tool()
    (ws_tmp / "src.txt").write_text(ORIG, encoding="utf-8")

    r = await tool.execute(
        {"action": "edit", "path": "src.txt",
         "start_line": 2, "end_line": 3,
         "content": "X2\nX3"},
        ctx,
    )
    assert r.success, r.error
    assert r.metadata["start_line"] == 2 and r.metadata["end_line"] == 3
    after = (ws_tmp / "src.txt").read_text(encoding="utf-8")
    assert after == "line1\nX2\nX3\nline4\nline5\n"
    assert r.metadata["diff_before"] == ORIG
    assert r.metadata["diff_after"] == after


@pytest.mark.asyncio
async def test_file_edit_single_line(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = make_tool()
    (ws_tmp / "src.txt").write_text(ORIG, encoding="utf-8")
    r = await tool.execute(
        {"action": "edit", "path": "src.txt",
         "start_line": 3, "end_line": 3, "content": "LINE3"},
        ctx,
    )
    assert r.success
    assert (ws_tmp / "src.txt").read_text(encoding="utf-8") == \
        "line1\nline2\nLINE3\nline4\nline5\n"


@pytest.mark.asyncio
async def test_file_edit_out_of_range_rejected(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = make_tool()
    (ws_tmp / "src.txt").write_text(ORIG, encoding="utf-8")
    r = await tool.execute(
        {"action": "edit", "path": "src.txt",
         "start_line": 1, "end_line": 99, "content": "boom"},
        ctx,
    )
    assert r.success is False
    assert "越界" in r.error or "行" in r.error
    # 文件未被修改
    assert (ws_tmp / "src.txt").read_text(encoding="utf-8") == ORIG


@pytest.mark.asyncio
async def test_file_edit_missing_file(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = make_tool()
    r = await tool.execute(
        {"action": "edit", "path": "nope.txt",
         "start_line": 1, "end_line": 1, "content": "x"},
        ctx,
    )
    assert r.success is False


@pytest.mark.asyncio
async def test_file_edit_blocked_in_read_only_role(ws_tmp):
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = make_tool(read_only=True)
    (ws_tmp / "src.txt").write_text(ORIG, encoding="utf-8")
    r = await tool.execute(
        {"action": "edit", "path": "src.txt",
         "start_line": 1, "end_line": 1, "content": "x"},
        ctx,
    )
    assert r.success is False
    assert "只读" in r.error