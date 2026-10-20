# -*- coding: utf-8 -*-
"""大项目增量快照：默认不全量哈希，仅跟踪 Agent 写入与关键配置。"""
from pathlib import Path

from agent.project_state import ProjectStateTracker


def test_large_tree_scan_hashes_only_tracked_and_key_configs(ws_tmp: Path):
    root = ws_tmp / "project"
    pkg = root / "src" / "pkg"
    pkg.mkdir(parents=True)
    for index in range(1050):
        (pkg / f"file_{index:04d}.py").write_text(
            f"VALUE = {index}\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "agent.yaml").write_text("agent: {}\n",
                                                encoding="utf-8")
    (root / "package.json").write_text('{"name": "demo"}\n',
                                       encoding="utf-8")

    tracker = ProjectStateTracker(str(root))
    snapshot = tracker.scan()

    sample = snapshot["structure"]["src/pkg/file_0000.py"]
    assert sample["size"] > 0
    assert "sha1" not in sample
    assert snapshot["structure"]["package.json"]["sha1"]
    assert snapshot["structure"]["config/agent.yaml"]["sha1"]
    assert snapshot["hash_policy"] == "incremental"
    assert snapshot["dirs"]["src/pkg"]["files"] == 1050
    assert snapshot["dirs"]["."]["files"] == 1052
    assert tracker.state["hash_paths"] == []


def test_full_hash_does_not_poison_incremental_policy(ws_tmp: Path):
    root = ws_tmp / "project"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    tracker = ProjectStateTracker(str(root))

    snapshot = tracker.scan(full_hash=True)
    assert snapshot["hash_policy"] == "full"
    assert snapshot["structure"]["a.py"]["sha1"]
    assert tracker.state["hash_paths"] == []


def test_agent_written_file_hash_persists_and_detects_content_change(ws_tmp: Path):
    root = ws_tmp / "project"
    root.mkdir()
    path = root / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    tracker = ProjectStateTracker(str(root))
    tracker.begin_session()

    path.write_text("x = 2\n", encoding="utf-8")
    tracker.note_agent_write("a.py")
    tracker.end_session()
    assert "a.py" in tracker.state["hash_paths"]

    tracker2 = ProjectStateTracker(str(root))
    snapshot = tracker2.scan()
    assert snapshot["structure"]["a.py"]["sha1"]
    tracker2.begin_session()
    path.write_text("x = 3\n", encoding="utf-8")

    tracker3 = ProjectStateTracker(str(root))
    diff = tracker3.begin_session()
    assert "a.py" in diff["files"]["modified"]
    assert tracker3.state["dirs"]["."]["files"] == 1
