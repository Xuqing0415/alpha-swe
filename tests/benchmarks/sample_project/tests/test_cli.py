"""CLI 端到端测试（真实子进程）。"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run_cli(db: Path, *args):
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "taskboard.cli",
         "--db", str(db), *args],
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=REPO, timeout=30,
    )


def test_cli_add_and_list(tmp_path):
    db = tmp_path / "tasks.json"
    r = run_cli(db, "add", "写周报", "--priority", "high", "--tags", "work,report")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["title"] == "写周报"
    assert data["priority"] == "high"

    r2 = run_cli(db, "list")
    assert r2.returncode == 0
    assert "写周报" in r2.stdout


def test_cli_complete_missing_returns_error(tmp_path):
    db = tmp_path / "tasks.json"
    r = run_cli(db, "complete", "nope")
    assert r.returncode == 1
    assert "任务不存在" in r.stderr
