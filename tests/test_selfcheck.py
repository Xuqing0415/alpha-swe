"""启动自检测试：run_selfcheck / critical_failed / format_selfcheck / find_free_port。

对应「命令行/TUI 驱动新核心系统性排查」1.3：启动自检必须覆盖
config/workspace/memory/sandbox/tools/observability，任何单项异常
都降级为 FAIL 项而不是向外抛异常。
"""
import socket
from pathlib import Path

from agent.config import AppConfig
from agent.selfcheck import (critical_failed, find_free_port,
                             format_selfcheck, run_selfcheck)


def _config(ws_tmp: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.sandbox.workspace = str(ws_tmp / "ws")
    return cfg


def test_run_selfcheck_default_config(ws_tmp):
    results = run_selfcheck(_config(ws_tmp))
    by_name = {r.name: r for r in results}
    assert set(by_name) == {"config", "workspace", "memory", "sandbox",
                            "tools", "observability"}
    assert by_name["config"].ok
    assert by_name["workspace"].ok
    assert by_name["sandbox"].ok
    assert by_name["tools"].ok
    assert not critical_failed(results)


def test_run_selfcheck_selective_checks(ws_tmp):
    results = run_selfcheck(_config(ws_tmp), checks=["config", "workspace"])
    assert [r.name for r in results] == ["config", "workspace"]


def test_run_selfcheck_critical_failure_detected(ws_tmp):
    cfg = _config(ws_tmp)
    # workspace 指向文件下的路径：mkdir 失败 -> 关键项 FAIL
    blocker = ws_tmp / "blocker"
    blocker.write_text("x", encoding="utf-8")
    cfg.sandbox.workspace = str(blocker / "ws")
    results = run_selfcheck(cfg)
    by_name = {r.name: r for r in results}
    assert not by_name["workspace"].ok
    assert by_name["workspace"].critical
    assert [r.name for r in critical_failed(results)] == ["workspace"]


def test_run_selfcheck_never_raises_on_broken_config():
    class Bad:
        pass

    results = run_selfcheck(Bad())
    assert results
    for r in results:
        assert not r.ok  # 缺属性也降级为 FAIL，不向外抛


def test_format_selfcheck_readable(ws_tmp):
    results = run_selfcheck(_config(ws_tmp))
    text = format_selfcheck(results)
    assert "启动自检" in text
    assert "[OK  ] config" in text
    assert "自检通过" in text


def test_find_free_port_skips_occupied(ws_tmp):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        free = find_free_port("127.0.0.1", port)
        assert free != port
        assert free > port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
            s2.bind(("127.0.0.1", free))


def test_find_free_port_all_busy_returns_zero():
    # max_tries=1 且指定端口被占用 -> 返回 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        assert find_free_port("127.0.0.1", port, max_tries=1) == 0
