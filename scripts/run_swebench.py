# -*- coding: utf-8 -*-
"""SWE-bench 评估命令行入口（方向二）。

用法:
    # 本地 JSONL 子集（推荐，可离线准备）
    python -X utf8 scripts/run_swebench.py --instances data/swebench_lite_20.jsonl \\
        --results-dir logs/swebench/run1 --max-parallel 2 --timeout 1800

    # HuggingFace 全量 Lite（需安装 datasets，首次会下载）
    python -X utf8 scripts/run_swebench.py --hf swe-bench-lite \\
        --max-instances 20 --seed 42 --results-dir logs/swebench/run2

    # 只跑 Agent 不评估（-no-eval），用于快速试跑
    python -X utf8 scripts/run_swebench.py --instances x.jsonl --no-eval
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from swe_eval.analyze import save_markdown_report
from swe_eval.dataset import (load_hf_subset, load_instances_file,
                              save_instances_jsonl, select_subset)
from swe_eval.adapter import SweAgentAdapter
from swe_eval.runner import SweBenchRunner, save_report


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -X utf8 scripts/run_swebench.py",
        description="SWE-bench 批量评估：Agent 求解 -> 测试评估 -> 报告",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--instances", help="本地实例文件（.json/.jsonl）")
    src.add_argument("--hf", choices=["swe-bench", "swe-bench-lite"],
                     help="HuggingFace 数据集名称")
    p.add_argument("--max-instances", type=int, default=None,
                   help="最多运行实例数")
    p.add_argument("--seed", type=int, default=None,
                   help="子集采样种子（保证可复现）")
    p.add_argument("--save-subset", default=None,
                   help="把选中的子集固化为 JSONL（后续离线复用）")
    p.add_argument("--results-dir", default="logs/swebench/run1",
                   help="结果目录（默认 logs/swebench/run1）")
    p.add_argument("--config", default=str(REPO_ROOT / "config" / "swebench.yaml"),
                   help="Agent 配置文件（默认 config/swebench.yaml）")
    p.add_argument("--max-parallel", type=int, default=2,
                   help="并发实例数（建议 1-2，避免拖垮机器）")
    p.add_argument("--timeout", type=float, default=1800.0,
                   help="单实例 Agent 超时（秒）")
    p.add_argument("--max-cost", type=float, default=None,
                   help="单实例 token 预算（美元）")
    p.add_argument("--max-tokens", type=int, default=None,
                   help="上下文 token 上限覆盖")
    p.add_argument("--docker", action="store_true",
                   help="启用 Docker 沙箱（需镜像 alphaswe/dev:latest）")
    p.add_argument("--no-eval", action="store_true",
                   help="跳过测试评估（只收集 patch）")
    p.add_argument("--eval-timeout", type=float, default=300.0,
                   help="单条测试超时（秒）")
    p.add_argument("--install-cmd", default=None,
                   help="评估前执行的依赖安装命令（如 'pip install -e .'）")
    p.add_argument("--keep-repos", action="store_true",
                   help="保留克隆的仓库（默认运行后清理）")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    results_dir = Path(args.results_dir)

    # 1) 加载并固定子集
    if args.instances:
        instances = load_instances_file(args.instances)
    else:
        hf_name = {"swe-bench": "princeton-nlp/SWE-bench",
                   "swe-bench-lite": "princeton-nlp/SWE-bench_Lite"}[args.hf]
        instances = load_hf_subset(hf_name, max_instances=None, seed=None)
    instances = select_subset(instances, args.max_instances or 0,
                              seed=args.seed)
    if not instances:
        print("没有可运行的实例，请检查输入。", file=sys.stderr)
        return 1
    print(f"选中 {len(instances)} 个实例，结果目录: {results_dir}")
    if args.save_subset:
        save_instances_jsonl(instances, args.save_subset)
        print(f"子集已固化: {args.save_subset}")

    # 2) 运行
    adapter = SweAgentAdapter(
        config_path=args.config, timeout=args.timeout,
        max_cost=args.max_cost, max_tokens=args.max_tokens,
        docker=args.docker,
    )
    runner = SweBenchRunner(
        adapter=adapter, results_dir=results_dir,
        max_parallel=args.max_parallel, evaluate=not args.no_eval,
        eval_timeout=args.eval_timeout, install_cmd=args.install_cmd,
        keep_repos=args.keep_repos,
    )
    results = runner.run_many(instances)

    # 3) 报告
    report = save_report(results_dir, instances, results)
    md_path = save_markdown_report(report, results, results_dir)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Markdown 报告: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
