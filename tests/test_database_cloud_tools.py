# -*- coding: utf-8 -*-
"""数据库与云 CLI 工具测试：安全策略（只读/confirm/危险命令）、超时、降级路径。"""
import asyncio
import sqlite3

import pytest

from agent.tools.base import ErrorCategory, ExecutionContext
from agent.tools.cloud_tool import CloudTool
from agent.tools.database_tool import DatabaseTool


def _ctx(ws_tmp):
    return ExecutionContext(workspace=str(ws_tmp))


def _make_sqlite_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO items (name) VALUES (?)",
                     [("alpha",), ("beta",)])
    conn.commit()
    conn.close()


class _DummyLogger:
    def __init__(self):
        self.records = []

    def record(self, key, *args, **kwargs):
        self.records.append(key)


@pytest.mark.asyncio
async def test_database_sqlite_read_only_select(ws_tmp):
    db = ws_tmp / "sample.db"
    _make_sqlite_db(db)
    tool = DatabaseTool()
    r = await tool.execute(
        {"engine": "sqlite", "path": str(db),
         "query": "SELECT name FROM items ORDER BY id"},
        _ctx(ws_tmp))
    assert r.success
    assert r.metadata["rows"] == 2
    assert r.metadata["columns"] == ["name"]
    assert "alpha" in r.output and "beta" in r.output


@pytest.mark.asyncio
async def test_database_write_blocked_by_default(ws_tmp):
    db = ws_tmp / "sample.db"
    _make_sqlite_db(db)
    tool = DatabaseTool()
    r = await tool.execute(
        {"engine": "sqlite", "path": str(db), "read_only": False,
         "query": "INSERT INTO items (name) VALUES ('x')", "confirm": True},
        _ctx(ws_tmp))
    assert r.success is False
    assert r.error_category == ErrorCategory.PERMISSION


@pytest.mark.asyncio
async def test_database_write_needs_confirm(ws_tmp):
    db = ws_tmp / "sample.db"
    _make_sqlite_db(db)
    tool = DatabaseTool(allow_write=True)
    r = await tool.execute(
        {"engine": "sqlite", "path": str(db), "read_only": False,
         "query": "INSERT INTO items (name) VALUES ('x')"},
        _ctx(ws_tmp))
    assert r.success is False
    assert r.error_category == ErrorCategory.PERMISSION


@pytest.mark.asyncio
async def test_database_write_confirmed_affects_rows(ws_tmp):
    db = ws_tmp / "sample.db"
    _make_sqlite_db(db)
    tool = DatabaseTool(allow_write=True)
    r = await tool.execute(
        {"engine": "sqlite", "path": str(db), "read_only": False,
         "confirm": True,
         "query": "INSERT INTO items (name) VALUES ('x')"},
        _ctx(ws_tmp))
    assert r.success
    assert r.metadata["affected"] == 1
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()
    assert n == 3


@pytest.mark.asyncio
async def test_database_missing_query(ws_tmp):
    tool = DatabaseTool()
    r = await tool.execute({"engine": "sqlite"}, _ctx(ws_tmp))
    assert r.success is False
    assert r.error_category == ErrorCategory.PERMANENT


@pytest.mark.asyncio
async def test_database_unsupported_engine(ws_tmp):
    tool = DatabaseTool()
    r = await tool.execute({"engine": "oracle", "query": "SELECT 1"},
                           _ctx(ws_tmp))
    assert r.success is False
    assert r.error_category == ErrorCategory.PERMANENT


@pytest.mark.asyncio
async def test_database_decision_logger(ws_tmp):
    db = ws_tmp / "sample.db"
    _make_sqlite_db(db)
    logger = _DummyLogger()
    tool = DatabaseTool(decision_logger=logger)
    r = await tool.execute(
        {"engine": "sqlite", "path": str(db), "query": "SELECT 1"},
        _ctx(ws_tmp))
    assert r.success
    assert "database.query" in logger.records


@pytest.mark.asyncio
async def test_database_timeout_transient(ws_tmp, monkeypatch):
    tool = DatabaseTool()

    def _slow(*args, **kwargs):
        import time
        time.sleep(5)
        return {"output": "late"}

    monkeypatch.setattr(DatabaseTool, "_run_sync", _slow)
    r = await tool.execute(
        {"engine": "sqlite", "path": "x.db", "query": "SELECT 1",
         "timeout": 0.2},
        _ctx(ws_tmp))
    assert r.success is False
    assert r.error_category == ErrorCategory.TRANSIENT


def test_database_format_rows_none_and_truncated():
    from agent.tools.database_tool import _format_rows

    out = _format_rows(["a", "b"], [(1, None), (2, "x")], truncated=True)
    lines = out.splitlines()
    assert len(lines) == 4
    assert lines[0] == "a\tb"
    assert lines[1] == "1\t"
    assert lines[3].startswith("...")


# ---- CloudTool ----

@pytest.mark.asyncio
async def test_cloud_unsupported_tool(ws_tmp):
    tool = CloudTool()
    r = await tool.execute(
        {"tool": "terraform", "args": ["--version"], "confirm": True},
        _ctx(ws_tmp))
    assert r.success is False
    assert r.error_category == ErrorCategory.PERMANENT


@pytest.mark.asyncio
async def test_cloud_requires_confirm(ws_tmp):
    tool = CloudTool()
    r = await tool.execute(
        {"tool": "aws", "args": ["sts", "get-caller-identity"]},
        _ctx(ws_tmp))
    assert r.success is False
    assert r.error_category == ErrorCategory.PERMISSION


@pytest.mark.asyncio
async def test_cloud_blocks_dangerous_words(ws_tmp):
    tool = CloudTool()
    r = await tool.execute(
        {"tool": "kubectl", "args": ["delete", "pod", "x"], "confirm": True},
        _ctx(ws_tmp))
    assert r.success is False
    assert r.error_category == ErrorCategory.PERMISSION


class _OkProc:
    returncode = 0

    async def communicate(self):
        return (b"aws-cli/2.0\n", None)

    def kill(self):
        pass


class _FailProc:
    returncode = 1

    async def communicate(self):
        return (b"error: denied", None)

    def kill(self):
        pass



@pytest.mark.asyncio
async def test_cloud_success_path(ws_tmp, monkeypatch):
    logger = _DummyLogger()
    tool = CloudTool(decision_logger=logger)

    async def _fake_exec(*args, **kwargs):
        return _OkProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    r = await tool.execute(
        {"tool": "aws", "args": ["--version"], "confirm": True},
        _ctx(ws_tmp))
    assert r.success
    assert "aws-cli/2.0" in r.output
    assert r.metadata["returncode"] == 0
    assert "cloud.exec" in logger.records


@pytest.mark.asyncio
async def test_cloud_nonzero_exit(ws_tmp, monkeypatch):
    tool = CloudTool()

    async def _fake_exec(*args, **kwargs):
        return _FailProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    r = await tool.execute(
        {"tool": "aws", "args": ["s3", "ls"], "confirm": True},
        _ctx(ws_tmp))
    assert r.success is False
    assert r.metadata["returncode"] == 1
    assert "error: denied" in r.output


@pytest.mark.asyncio
async def test_cloud_timeout_kills_proc(ws_tmp, monkeypatch):
    tool = CloudTool()
    killed = []

    class _HangProc:
        returncode = 1

        async def communicate(self):
            await asyncio.sleep(5)
            return (b"", None)

        def kill(self):
            killed.append(True)

    async def _fake_exec(*args, **kwargs):
        return _HangProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    r = await tool.execute(
        {"tool": "az", "args": ["version"], "confirm": True,
         "timeout": 0.1},
        _ctx(ws_tmp))
    assert r.success is False
    assert r.error_category == ErrorCategory.TRANSIENT
    assert killed == [True]


@pytest.mark.asyncio
async def test_cloud_missing_binary(ws_tmp, monkeypatch):
    tool = CloudTool()

    async def _raise(*args, **kwargs):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)
    r = await tool.execute(
        {"tool": "docker", "args": ["info"], "confirm": True},
        _ctx(ws_tmp))
    assert r.success is False
    assert r.error_category == ErrorCategory.CONFIGURATION


@pytest.mark.asyncio
async def test_cloud_spawn_generic_failure(ws_tmp, monkeypatch):
    tool = CloudTool()

    async def _raise(*args, **kwargs):
        raise RuntimeError("spawn boom")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)
    r = await tool.execute(
        {"tool": "gcloud", "args": ["version"], "confirm": True},
        _ctx(ws_tmp))
    assert r.success is False
    assert r.error_category == ErrorCategory.TRANSIENT