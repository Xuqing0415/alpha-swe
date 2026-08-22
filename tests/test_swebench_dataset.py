# -*- coding: utf-8 -*-
"""swe_eval.dataset：JSONL 加载、子集采样、prompt 组合、仓库名。"""
import json

import pytest

from swe_eval.dataset import (Instance, _run_git, load_instances_file,
                              repo_dir_name, save_instances_jsonl,
                              select_subset)


def _instance(iid: str = "test__repo-1") -> Instance:
    return Instance(
        instance_id=iid,
        repo="octocat/hello",
        base_commit="abc123",
        problem_statement="Fix the bug.",
        patch="--- a/x.py\n+++ b/x.py\n",
        test_patch="--- a/tests/test_x.py\n+++ b/tests/test_x.py\n",
        fail_to_pass=["tests/test_x.py::test_bug"],
        pass_to_pass=["tests/test_x.py::test_ok"],
        hints_text="Look at x.py.",
    )


def test_instance_prompt_includes_hints():
    inst = _instance()
    p = inst.prompt
    assert "Fix the bug." in p
    assert "参考提示" in p and "Look at x.py." in p


def test_instance_roundtrip_dict():
    inst = _instance()
    d = inst.to_dict()
    assert d["FAIL_TO_PASS"] == ["tests/test_x.py::test_bug"]
    assert "extra" not in d or d["extra"] == {}
    inst2 = Instance.from_dict(d)
    assert inst2.instance_id == inst.instance_id
    assert inst2.fail_to_pass == inst.fail_to_pass
    assert inst2.all_test_nodes == [
        "tests/test_x.py::test_bug", "tests/test_x.py::test_ok"]


def test_load_save_jsonl_roundtrip(ws_tmp):
    path = ws_tmp / "subset.jsonl"
    save_instances_jsonl([_instance(), _instance("b__repo-2")], path)
    loaded = load_instances_file(path)
    assert len(loaded) == 2
    assert loaded[0].repo == "octocat/hello"
    assert loaded[1].instance_id == "b__repo-2"


def test_load_json_array(ws_tmp):
    path = ws_tmp / "arr.json"
    path.write_text(json.dumps([_instance().to_dict()]), encoding="utf-8")
    loaded = load_instances_file(path)
    assert len(loaded) == 1


def test_select_subset_reproducible():
    insts = [_instance(f"i-{i}") for i in range(20)]
    a = select_subset(insts, 5, seed=42)
    b = select_subset(insts, 5, seed=42)
    assert [x.instance_id for x in a] == [x.instance_id for x in b]
    assert len(a) == 5


def test_select_subset_keeps_order_without_seed():
    insts = [_instance(f"i-{i}") for i in range(20)]
    sub = select_subset(insts, 5)
    assert [x.instance_id for x in sub] == [f"i-{i}" for i in range(5)]


def test_invalid_jsonl_raises(ws_tmp):
    path = ws_tmp / "bad.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_instances_file(path)


def test_run_git_without_cwd():
    # 回归：cwd=None 时不得传 cwd="None"，否则 Windows 抛 WinError 267
    proc = _run_git(["version"])
    assert proc.returncode == 0


def test_run_git_with_cwd(ws_tmp):
    proc = _run_git(["version"], cwd=ws_tmp)
    assert proc.returncode == 0


def test_git_env_bypasses_unreachable_proxy(monkeypatch):
    import swe_eval.dataset as d
    monkeypatch.setattr(d, "_proxy_unreachable", lambda: True)
    d._proxy_bypass = None
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    env = d._git_env()
    assert "HTTPS_PROXY" not in env
    d._proxy_bypass = None


def test_git_env_keeps_reachable_proxy(monkeypatch):
    import swe_eval.dataset as d
    monkeypatch.setattr(d, "_proxy_unreachable", lambda: False)
    d._proxy_bypass = None
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    env = d._git_env()
    assert env.get("HTTPS_PROXY") == "http://proxy.local:3128"
    d._proxy_bypass = None


def test_repo_dir_name():
    inst = _instance('django__django-11099')
    inst.repo = 'django/django'
    assert repo_dir_name(inst) == "django__django__django__django-11099"
