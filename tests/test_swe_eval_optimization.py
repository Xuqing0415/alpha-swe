"""方向一 1.2/2.1：实验记录、配置覆盖、失败归因增强与案例库（离线单测）。"""
import json

import pytest

from swe_eval.adapter import AdapterResult
from swe_eval.analyze import (export_case_library, refine_category,
                              trajectory_signals)
from swe_eval.experiments import (apply_config_overrides, append_experiment_log,
                                  config_hash, load_experiment_log,
                                  summarize_results)


def _results():
    return [
        {"instance_id": "a", "status": "resolved",
         "adapter": {"tokens": 100, "rounds": 2, "llm_calls": 3,
                     "files_modified": ["f.py"]}},
        {"instance_id": "b", "status": "unresolved",
         "adapter": {"tokens": 50, "rounds": 4, "llm_calls": 5,
                     "files_modified": []}},
        {"instance_id": "c", "status": "timeout",
         "adapter": {"tokens": 10, "rounds": 1, "llm_calls": 1,
                     "files_modified": []}},
    ]


def test_apply_config_overrides():
    base = ("agent:\n  max_rounds: 40\n"
            "context:\n  max_tokens: 8000\n")
    out = apply_config_overrides(
        base, {"context.max_tokens": "12000",
               "agent.recommend_files_enabled": "true"})
    import yaml
    data = yaml.safe_load(out)
    assert data["context"]["max_tokens"] == 12000
    assert data["agent"]["recommend_files_enabled"] is True
    assert data["agent"]["max_rounds"] == 40


def test_experiment_log_roundtrip(ws_tmp):
    log = str(ws_tmp / "exp.jsonl")
    cfg = ws_tmp / "c.yaml"
    cfg.write_text("context:\n  max_tokens: 8000\n", encoding="utf-8")
    rec = append_experiment_log(
        log, tag="baseline", config_path=str(cfg),
        config_overrides={"context.max_tokens": "12000"},
        subset_path="data/swebench/swebench_subset_50.json",
        results=_results())
    assert rec["config_hash"] == config_hash(str(cfg))
    assert rec["config_overrides"] == {"context.max_tokens": "12000"}
    assert rec["summary"]["resolve_rate"] == pytest.approx(1 / 3, abs=1e-4)
    rows = load_experiment_log(log)
    assert len(rows) == 1 and rows[0]["tag"] == "baseline"


def test_summarize_results():
    s = summarize_results(_results())
    assert s["total"] == 3 and s["resolved"] == 1
    assert s["status_counts"] == {"resolved": 1, "unresolved": 1,
                                  "timeout": 1}
    assert s["failure_categories"]["timeout"] == 1
    assert s["failed_ids"] == ["b", "c"]


def test_refine_category_and_signals():
    r = {"status": "unresolved",
         "adapter": {"rounds": 5, "files_modified": []}}
    assert refine_category(r) == "retrieval"
    r2 = {"status": "unresolved",
          "adapter": {"rounds": 5, "files_modified": ["a.py"]},
          "eval": {"resolved": False, "error": "test env error"}}
    assert refine_category(r2) == "test"
    sig = trajectory_signals(r2)
    assert sig["files_modified"] == ["a.py"]
    assert sig["rounds"] == 5


def test_export_case_library(ws_tmp):
    results_dir = ws_tmp / "run"
    results_dir.mkdir()
    out = export_case_library(_results(), results_dir)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 2  # resolved 被排除
    categories = [c["category"] for c in data]
    assert "retrieval" in categories and "timeout" in categories
    assert (results_dir / "case_library.md").exists()


def test_adapter_to_dict_includes_attribution():
    r = AdapterResult(
        instance_id="x", ok=False, status="failed", exit_code=1,
        payload={"attribution": {"category": "retrieval",
                                 "reason": "未定位到文件"},
                 "final_answer": "done"})
    d = r.to_dict()
    assert d["attribution"] == "retrieval"
    assert d["attribution_reason"] == "未定位到文件"
    assert d["final_answer"] == "done"
