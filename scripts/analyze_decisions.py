#!/usr/bin/env python3
"""决策日志分析工具 —— 验证配置是否真正影响 Agent 行为。

用法:
    python -X utf8 scripts/analyze_decisions.py [log_path]

log_path 缺省取环境变量 DECISION_LOG_PATH，再缺省为 decision_log.jsonl。
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

EXPECTED_KEYS = [
    "llm.provider", "llm.temperature", "llm.model",
    "sandbox.network_enabled", "sandbox.memory_limit", "sandbox.cpu_limit",
    "sandbox.timeout_seconds", "sandbox.read_only_root",
    "planner.max_subtasks", "planner.split_threshold_complexity",
    "planner.allow_parallel",
    "context.max_tokens", "context.compression_threshold",
    "context.compression_method",
    "memory.backend", "memory.top_k_retrieval", "memory.similarity_threshold",
    "agent.max_loop_iterations", "agent.parallel_tool_calls",
    "agent.require_confirmation", "agent.auto_approve",
    "active_skills",
    "plugin.enabled", "plugin.max_active", "active_plugins",
    "skills.enabled", "skills.workflow_enabled", "skills.allow_fallback",
    "team.roles", "team.read_only", "team.max_review_retries",
    "team.message_timeout",
    "sandbox.network_policy", "sandbox.protected_paths",
    "sandbox.resource_monitor", "sandbox.memory_limit_mb",
    "sandbox.docker_enabled", "sandbox.workdir", "sandbox.snapshot_prefix",
    "sandbox.auto_rollback", "sandbox.timeout_seconds",
    "mcp.reconnect_attempts", "mcp.resource_cache_ttl",
]


def load_decisions(log_path: str) -> List[Dict]:
    """读取 JSONL 决策日志。"""
    decisions: List[Dict] = []
    p = Path(log_path)
    if not p.exists():
        print(f"⚠️  日志不存在: {p}")
        return decisions
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                decisions.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return decisions


def analyze(log_path: str) -> Dict[str, Dict]:
    """按配置项聚合决策，输出配置影响分析报告。"""
    decisions = load_decisions(log_path)
    config_impacts: Dict[str, Dict] = defaultdict(
        lambda: {"count": 0, "decisions": set()}
    )
    for dp in decisions:
        key = dp["config_key"]
        config_impacts[key]["count"] += 1
        config_impacts[key]["decisions"].add(dp["decision"])

    print("=" * 60)
    print(f"配置影响分析报告: {log_path}（{len(decisions)} 条决策）")
    print("=" * 60)
    for key, data in sorted(config_impacts.items()):
        print(f"\n配置项: {key}")
        print(f"  决策次数: {data['count']}")
        print(f"  不同行为: {len(data['decisions'])}")
        for d in sorted(data["decisions"]):
            print(f"    - {d}")

    missing = [k for k in EXPECTED_KEYS if k not in config_impacts]
    print("\n" + "=" * 60)
    print("未生效配置项（无决策记录）:")
    if not missing:
        print("  ✅ 全部期望配置项均产生了决策")
    for key in missing:
        print(f"  ⚠️   {key} - 未产生任何决策")
    print("=" * 60)
    return config_impacts


def main() -> None:
    parser = argparse.ArgumentParser(description="分析决策日志")
    parser.add_argument("log_path", nargs="?", default=None,
                        help="JSONL 日志路径（缺省用 DECISION_LOG_PATH 或 decision_log.jsonl）")
    args = parser.parse_args()
    log_path = args.log_path or os.environ.get("DECISION_LOG_PATH", "decision_log.jsonl")
    analyze(log_path)


if __name__ == "__main__":
    main()