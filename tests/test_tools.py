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
async def test_file_search_skips_excluded_dirs(ws_tmp):
    """方案 3.1：搜索跳过 node_modules/.git/__pycache__ 等目录。"""
    from agent.tools import fileio as fileio_mod

    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = FileIOTool()
    (ws_tmp / "src").mkdir(parents=True)
    (ws_tmp / "src" / "app.py").write_text("target_symbol\n", encoding="utf-8")
    (ws_tmp / "node_modules").mkdir()
    (ws_tmp / "node_modules" / "dep.py").write_text("target_symbol\n", encoding="utf-8")
    (ws_tmp / "__pycache__").mkdir()
    (ws_tmp / "__pycache__" / "cache.py").write_text("target_symbol\n", encoding="utf-8")

    r = await tool.execute({"action": "search", "path": ".",
                            "pattern": "target_symbol"}, ctx)
    assert r.success
    assert r.metadata["hits"] == 1  # 只有 src/app.py 命中
    assert "app.py" in r.output and "node_modules" not in r.output


@pytest.mark.asyncio
async def test_file_search_caps_and_marks_truncated(ws_tmp, monkeypatch):
    """方案 3.1：超过上限只返回前 N 条并给出统计提示。"""
    from agent.tools import fileio as fileio_mod

    monkeypatch.setattr(fileio_mod, "SEARCH_RESULT_CAP", 5)
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = FileIOTool()
    for i in range(8):
        (ws_tmp / f"f{i}.py").write_text("needle\n", encoding="utf-8")
    r = await tool.execute({"action": "search", "path": ".",
                            "pattern": "needle"}, ctx)
    assert r.success
    assert r.metadata["hits"] == 8
    assert r.metadata["truncated"] is True
    assert "共 8 处，仅显示前 5 条" in r.output
    assert r.output.count("f") >= 5  # 至少 5 条结果


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

def test_terminal_decode_utf8_and_gbk_fallback():
    """终端输出解码：UTF-8 直通，GBK 字节自动回退，杜绝乱码。"""
    assert TerminalTool._decode("中文".encode("utf-8")) == "中文"
    assert TerminalTool._decode("测试".encode("gbk")) == "测试"


def test_terminal_build_argv_forces_utf8_on_windows():
    """Windows 上 PowerShell 命令应强制 UTF-8 输出（避免 GBK 乱码）。"""
    tool = TerminalTool()
    argv = tool._build_argv("dir")
    if argv[0].lower() == "powershell":
        joined = " ".join(argv)
        assert "[Console]::OutputEncoding" in joined
        assert "chcp 65001" in joined


@pytest.mark.asyncio
async def test_fileio_write_metadata_has_diff_snapshot(ws_tmp):
    """写入/追加返回 diff_before/diff_after 快照，供 TUI 渲染 unified diff。"""
    ctx = ExecutionContext(workspace=str(ws_tmp))
    tool = FileIOTool()
    r = await tool.execute({"action": "write", "path": "src/app.py",
                            "content": "def main():\n    pass\n"}, ctx)
    assert r.success
    assert r.metadata["diff_before"] is None  # 新建文件
    assert r.metadata["diff_after"] == "def main():\n    pass\n"

    r = await tool.execute({"action": "write", "path": "src/app.py",
                            "content": "def main():\n    print(1)\n"}, ctx)
    assert r.success
    assert r.metadata["diff_before"] == "def main():\n    pass\n"
    assert "print(1)" in r.metadata["diff_after"]

    r = await tool.execute({"action": "append", "path": "src/app.py",
                            "content": "# end\n"}, ctx)
    assert r.success
    assert "print(1)" in r.metadata["diff_before"]
    assert "# end" in r.metadata["diff_after"]

