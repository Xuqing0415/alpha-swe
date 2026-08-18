# -*- coding: utf-8 -*-
"""实验记录（方向一·阶段一 1.2）。

把每次 SWE-bench 运行固化为一条 JSONL 实验记录：配置哈希、配置覆盖、
子集、解决率、资源消耗与失败归因摘要，支持横向对比 A/B。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from swe_eval.analyze import classify_failures


def config_hash(config_path: Optional[str]) -> str:
    """配置文件的 sha256 前 16 位，用于 A/B 追踪。"""
    if not config_path:
        return ""
    try:
        return hashlib.sha256(
            Path(config_path).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def summarize_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """聚合一批实例结果为实验摘要（与 save_report.summary 字段对齐）。"""
    total = len(results)
    resolved = sum(1 for r in results if r.get("status") == "resolved")
    status_counts: Dict[str, int] = {}
    for r in results:
        s = str(r.get("status") or "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    def avg(key: str) -> float:
        if not total:
            return 0.0
        vals = [(r.get("adapter") or {}).get(key) or 0 for r in results]
        return round(sum(vals) / total, 2)

    return {
        "total": total,
        "resolved": resolved,
        "resolve_rate": round(resolved / total, 4) if total else 0.0,
        "status_counts": status_counts,
        "failure_categories": classify_failures(results),
        "avg_tokens": avg("tokens"),
        "avg_elapsed_s": avg("elapsed_s"),
        "avg_rounds": avg("rounds"),
        "failed_ids": [str(r.get("instance_id")) for r in results
                       if r.get("status") != "resolved"],
    }


def append_experiment_log(
    log_path: str,
    *,
    tag: str,
    config_path: Optional[str] = None,
    config_overrides: Optional[Dict[str, str]] = None,
    subset_path: Optional[str] = None,
    results: Optional[List[Dict[str, Any]]] = None,
    summary: Optional[Dict[str, Any]] = None,
    notes: str = "",
) -> Dict[str, Any]:
    """追加一条实验记录（JSONL），返回该记录。"""
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tag": tag,
        "config": str(config_path) if config_path else "",
        "config_hash": config_hash(config_path),
        "config_overrides": dict(config_overrides or {}),
        "subset": str(subset_path) if subset_path else "",
        "summary": summary or summarize_results(results or []),
        "notes": notes,
    }
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def load_experiment_log(log_path: str) -> List[Dict[str, Any]]:
    """读取实验日志全部记录（按时间顺序）。"""
    rows: List[Dict[str, Any]] = []
    with Path(log_path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _coerce(raw: str):
    raw = raw.strip()
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def apply_config_overrides(base_yaml: str,
                           overrides: Dict[str, str]) -> str:
    """把 a.b.c=value 覆盖合并进 YAML 文本（实验 A/B 用，生成临时配置）。"""
    import yaml
    data = yaml.safe_load(base_yaml) or {}
    for dotted, raw in overrides.items():
        parts = [p for p in dotted.split(".") if p]
        if not parts:
            continue
        node = data
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = _coerce(raw)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
