# -*- coding: utf-8 -*-
"""读取 pytest-benchmark JSON 并断言性能基线上限（边界打磨第 6 步）。

chaos-stage 先跑
    python -X utf8 -m pytest tests/test_perf_baseline.py \
        --benchmark-autosave --benchmark-json=<path>
再用本脚本断言四个关键操作的 mean 耗时不超过宽松上限，防止数量级性能
回归（上限默认 1000ms，远高于本地实测的 ~1.7-77ms，抗 runner 抖动）。

用法：
    python scripts/check_perf_baseline.py <benchmark-json> [ceiling_ms]

退出码：0 全部达标；1 缺失用例或任一用例 mean 超限。
"""
import json
import os
import sys

REQUIRED_SUFFIXES = (
    "test_bench_state_advance",
    "test_bench_parser_tool_json",
    "test_bench_policy_command_gate",
    "test_bench_path_resolve",
)


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    json_path = argv[1]
    ceiling_ms = float(argv[2]) if len(argv) > 2 else float(
        os.environ.get("PERF_CEILING_MS", "1000"))

    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    names = [b["name"] for b in data.get("benchmarks", [])]
    missing = [s for s in REQUIRED_SUFFIXES
               if not any(s in n for n in names)]
    if missing:
        print("[FAIL] 缺失性能基线用例: %s" % missing)
        return 1

    failed = False
    for b in data["benchmarks"]:
        short = b["name"].split("::")[-1]
        if not any(s in short for s in REQUIRED_SUFFIXES):
            continue
        mean_ms = b["stats"]["mean"] * 1000
        ok = mean_ms <= ceiling_ms
        failed = failed or not ok
        print("[%s] %-36s mean=%8.2fms  ceiling=%8.0fms"
              % ("OK" if ok else "FAIL", short, mean_ms, ceiling_ms))
    if failed:
        print("[FAIL] 性能基线超出上限（ceiling=%sms）" % ceiling_ms)
        return 1
    print("[PASS] 性能基线上限检查通过（ceiling=%sms）" % ceiling_ms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
