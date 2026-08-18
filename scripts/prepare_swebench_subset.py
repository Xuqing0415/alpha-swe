# -*- coding: utf-8 -*-
"""生成可复现的 SWE-bench 评估子集（方向一·阶段一 1.3）。

用法:
    # 从本地 JSONL/JSON 选 50 个（固定种子）
    python -X utf8 scripts/prepare_swebench_subset.py --instances data/swebench_lite.jsonl \
        --count 50 --seed 42 --save data/swebench/swebench_subset_50.json

    # 从 HuggingFace 拉取 Lite 并固定子集（需 pip install datasets）
    python -X utf8 scripts/prepare_swebench_subset.py --hf swe-bench-lite \
        --count 50 --seed 42 --save data/swebench/swebench_subset_50.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from swe_eval.dataset import (load_hf_subset, load_instances_file,
                              save_instances_jsonl, select_subset)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -X utf8 scripts/prepare_swebench_subset.py",
        description="固化可复现的 SWE-bench 评估子集",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--instances", help="本地实例文件（.json/.jsonl）")
    src.add_argument("--hf", choices=["swe-bench", "swe-bench-lite"],
                     help="HuggingFace 数据集名称")
    p.add_argument("--count", type=int, default=50, help="子集大小（默认 50）")
    p.add_argument("--seed", type=int, default=42, help="固定种子（默认 42）")
    p.add_argument("--save", required=True, help="输出 JSONL 路径")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.instances:
        instances = load_instances_file(args.instances)
    else:
        hf_name = {"swe-bench": "princeton-nlp/SWE-bench",
                   "swe-bench-lite": "princeton-nlp/SWE-bench_Lite"}[args.hf]
        instances = load_hf_subset(hf_name)
    subset = select_subset(instances, args.count, seed=args.seed)
    if not subset:
        print("子集为空，请检查输入。", file=sys.stderr)
        return 1
    out = save_instances_jsonl(subset, args.save)
    print(f"已保存 {len(subset)} 个实例 -> {out}（seed={args.seed}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
