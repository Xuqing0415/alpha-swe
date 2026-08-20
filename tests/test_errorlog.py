"""统一错误出口测试：write_error_log / print_error / guard 装饰器。

对应「命令行/TUI 驱动新核心系统性排查」1.1：CLI/TUI 最外层异常
捕获后必须落盘全量 traceback、附带上下文摘要、且写入失败时绝不影响主流程。
"""
from pathlib import Path

from agent.errorlog import guard, print_error, write_error_log


def test_write_error_log_writes_full_traceback(ws_tmp):
    try:
        raise ValueError("boom-中文")
    except ValueError as e:
        path = write_error_log(e, context={"module": "test", "task": "t0"},
                               log_dir=str(ws_tmp))
    assert path
    text = Path(path).read_text(encoding="utf-8")
    assert "ValueError" in text
    assert "boom-中文" in text
    assert "traceback:" in text
    assert "context:" in text
    assert "module: test" in text
    assert "task: t0" in text


def test_write_error_log_session_suffix(ws_tmp):
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        path = write_error_log(e, session_id="abc123", log_dir=str(ws_tmp))
    assert path.endswith("_abc123.log")


def test_write_error_log_never_raises(ws_tmp):
    """log_dir 指向一个文件时 mkdir 失败，应返回空串而非抛异常。"""
    blocker = ws_tmp / "blocker"
    blocker.write_text("x", encoding="utf-8")
    path = write_error_log(RuntimeError("x"), log_dir=str(blocker))
    assert path == ""


def test_print_error_writes_stderr(capsys, ws_tmp):
    try:
        raise KeyError("k")
    except KeyError as e:
        print_error(e, context={"module": "t"}, log_path=str(ws_tmp / "e.log"))
    err = capsys.readouterr().err
    assert "KeyError" in err
    assert "module: t" in err
    assert "Traceback" in err


def test_guard_returns_exit_code_and_logs(ws_tmp, monkeypatch):
    monkeypatch.chdir(ws_tmp)

    @guard(exit_code=3)
    def boom():
        raise RuntimeError("fail")

    rc = boom()
    assert rc == 3
    logs = list((ws_tmp / "logs").glob("cli_error_*.log"))
    assert logs
    assert "RuntimeError" in logs[0].read_text(encoding="utf-8")
