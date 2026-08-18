# -*- coding: utf-8 -*-
"""swe_eval.evaluate：patch 应用与测试执行评分。"""
import subprocess
from pathlib import Path

from swe_eval.dataset import Instance
from swe_eval.evaluate import apply_patch, evaluate_patch


def _git(repo: Path, *args: str):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _make_repo(root: Path) -> Path:
    """base 版本有一个 off-by-one bug：add 返回 a + b - 1。"""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "calc.py").write_text(
        "def add(a, b):\n    return a + b - 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _instance(base_commit: str, test_patch: str) -> Instance:
    return Instance(
        instance_id="pytest__demo-1", repo="demo/demo",
        base_commit=base_commit, problem_statement="fix add",
        test_patch=test_patch,
        fail_to_pass=["tests/test_calc.py::test_add"],
        pass_to_pass=["tests/test_calc.py::test_present"],
    )


def _make_test_patch(repo: Path) -> str:
    """在仓库内创建测试文件，用 git diff 生成标准补丁后还原。"""
    test_file = repo / "tests" / "test_calc.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "from calc import add\n"
        "\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "\n"
        "def test_present():\n"
        "    assert add(0, 0) == 0\n",
        encoding="utf-8")
    _git(repo, "add", "-N", "tests/test_calc.py")
    patch = _git(repo, "diff", "--no-ext-diff", "--no-color",
                 "--", "tests/test_calc.py").stdout
    _git(repo, "checkout", "--quiet", "--", "tests")
    _git(repo, "clean", "-qf", "--", "tests")
    return patch


def _fix_patch() -> str:
    return (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b - 1\n"
        "+    return a + b\n"
    )


def _bad_patch() -> str:
    return (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b - 1\n"
        "+    return a + b + 99\n"
    )


def test_apply_patch_ok(ws_tmp):
    repo = _make_repo(ws_tmp / "repo")
    ok, note = apply_patch(repo, _fix_patch())
    assert ok
    assert "a + b\n" in (repo / "calc.py").read_text(encoding="utf-8")


def test_apply_patch_empty():
    ok, note = apply_patch(Path("."), "")
    assert ok


def test_evaluate_patch_resolved(ws_tmp):
    repo = _make_repo(ws_tmp / "repo")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    inst = _instance(base, _make_test_patch(repo))
    result = evaluate_patch(repo, inst, _fix_patch(),
                            eval_ws=ws_tmp / "eval_ws")
    assert result["resolved"] is True
    assert result["apply_ok"] is True
    assert result["tests_passed"] == 2
    assert result["tests_failed"] == 0


def test_evaluate_patch_unresolved(ws_tmp):
    repo = _make_repo(ws_tmp / "repo")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    inst = _instance(base, _make_test_patch(repo))
    result = evaluate_patch(repo, inst, _bad_patch(),
                            eval_ws=ws_tmp / "eval_ws")
    assert result["resolved"] is False
    assert result["tests_passed"] == 0
    assert result["tests_failed"] == 2
