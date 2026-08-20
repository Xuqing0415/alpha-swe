# SWE-bench 固定评估子集

本目录用于固化可复现的 SWE-bench 评估子集，
供方向一（SWE-bench 优化）快速迭代与基线对比。

## 子集文件

- `../swebench_lite_20.jsonl`（已固化）：seed=42 从 SWE-bench_Lite test
  抽出的 20 个实例，可直接 `--instances data/swebench_lite_20.jsonl` 运行。
- `swebench_subset_50.json`（预期）：固定 50 个实例的流行式子集，
  按下方方式 B 生成即可。

## 再生成方法（结果可复现）

```powershell
# 方式 A：从已有本地数据集选取（推荐，离线可用）
python -X utf8 scripts/prepare_swebench_subset.py --instances <your-swebench-lite.jsonl> `
    --count 50 --seed 42 --save data/swebench/swebench_subset_50.json

# 方式 B：从 HuggingFace 拉取并选取（需 pip install datasets）
python -X utf8 scripts/prepare_swebench_subset.py --hf swe-bench-lite `
    --count 50 --seed 42 --save data/swebench/swebench_subset_50.json
```

## 为什么用固定子集

- 保证每次优化 A/B 都在相同任务上比较，避免抽样误差干扰判断；
- 子集运行快、token 成本可控，适合快速迭代；
- 最终在确认最佳配置后再跑全量 Lite（约 300 个）。

## 实验记录

每次运行通过 `--experiment-log logs/swebench/experiments.jsonl --experiment-tag <name>` 记录，
包含配置哈希、配置覆盖、子集、解决率与失败归因摘要；
用 `--config-override a.b.c=value` 做 A/B 对比（如 `context.max_tokens=12000`）。
