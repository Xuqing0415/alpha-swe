# -*- coding: utf-8 -*-
"""swe_eval.adapter：统一接口包装与 patch 提取。"""
import json
import subprocess
from pathlib import Path

from swe_eval.adapter import (SweAgentAdapter, extract_json_payload,
                              extract_patch)
from swe_eval.dataset import Instance


def _git(repo: Path, *args: str):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _make_repo(root: Path, files: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for name, content in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_extract_json_payload():
    text = 'WARNING: x\n{"ok": true, "tokens": 12}\ntrailing'
    assert extract_json_payload(text) == {"ok": True, "tokens": 12}
    assert extract_json_payload("no json") is None
    assert extract_json_payload('{"a": {"b": 1}} tail') == {"a": {"b": 1}}


def test_extract_patch_includes_modified_and_new_files(ws_tmp):
    repo = _make_repo(ws_tmp / "repo", {"calc.py": "def add(a, b):\n    return a + b\n"})
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b + 1\n", encoding="utf-8")
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    patch = extract_patch(repo)
    assert "calc.py" in patch
    assert "new.py" in patch
    assert "+1" in patch


def _fake_runner(payload: dict, returncode: int = 0):
    def runner(cmd):
        return subprocess.CompletedProcess(
            cmd, returncode, stdout=json.dumps(payload), stderr="")
    return runner


def test_adapter_solve_writes_patch(ws_tmp):
    repo = _make_repo(ws_tmp / "repo", {"a.py": "def f():\n    return 1\n"})
    (repo / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    inst = Instance(instance_id="x__y-1", repo="r/r", base_commit="deadbeef",
                    problem_statement="change it")
    adapter = SweAgentAdapter(
        config_path=ws_tmp / "cfg.yaml",
        runner=_fake_runner({"ok": True, "status": "completed", "tokens": 100,
                             "rounds": 3, "elapsed_s": 1.5,
                             "files_modified": ["a.py"]}))
    patch_path = ws_tmp / "patch.diff"
    result = adapter.solve_instance(inst, repo, patch_path)
    assert result.instance_id == "x__y-1"
    assert result.ok is True
    assert result.tokens == 100
    assert "a.py" in result.patch
    assert patch_path.exists()
    assert "a.py" in patch_path.read_text(encoding="utf-8")


def test_adapter_failed_payload(ws_tmp):
    repo = _make_repo(ws_tmp / "repo", {"a.py": "x = 1\n"})
    inst = Instance(instance_id="x__y-2", repo="r/r", base_commit="deadbeef",
                    problem_statement="change it")
    adapter = SweAgentAdapter(
        config_path=ws_tmp / "cfg.yaml",
        runner=_fake_runner({"ok": False, "status": "failed",
                             "error": "解析失败"}, returncode=1))
    result = adapter.solve_instance(inst, repo)
    assert result.ok is False
    assert result.status == "failed"
    assert "解析失败" in result.error


def test_adapter_timeout(ws_tmp):
    def runner(cmd):
        raise subprocess.TimeoutExpired(cmd, timeout=1)
    adapter = SweAgentAdapter(config_path=ws_tmp / "cfg.yaml", runner=runner)
    result = adapter.solve("hi", ws_tmp)
    assert result.status == "timeout"
    assert "超时" in result.error
