# 方向二：SWE-bench 基准评估指南

本文档说明如何用 Alpha-SWE Agent 在 SWE-bench（及 Lite 子集）上跑通评估、
建立基线、分析失败并持续优化。对应 `swe_eval/` 包与 `scripts/run_swebench.py`。

## 1. 已实现的能力

| 模块 | 职责 |
| --- | --- |
| `swe_eval/dataset.py` | 实例加载（本地 JSONL / HuggingFace）、固定种子子集、仓库克隆与 base_commit 检出 |
| `swe_eval/adapter.py` | `SweAgentAdapter`：issue 文本 + 仓库 -> 统一 diff patch（子进程运行 `python -m agent run --output json`） |
| `swe_eval/evaluate.py` | 干净快照（`git archive` + tarfile）上应用 Agent patch 与 test_patch，逐条运行 pytest 判定 resolved |
| `swe_eval/runner.py` | 批量执行（线程池 + 并发上限）、逐实例归档、`results.jsonl` 实时落盘、指标聚合 |
| `swe_eval/analyze.py` | 失败分类（planning/retrieval/understanding/modification/context/tool/test/budget/timeout）、Markdown 报告 |
| `scripts/run_swebench.py` | CLI 入口 |
| `config/swebench.yaml` | 基准运行配置（120k token 预算、允许网络装依赖、注入仓库结构摘要） |

## 2. 快速开始

```powershell
# 1) 准备 20 个实例的子集（两种方式选一）
#    方式 A：官方导出 JSONL（任意来源，离线可用）
python -X utf8 scripts/run_swebench.py --instances data/swebench_lite_20.jsonl `
    --results-dir logs/swebench/run1 --max-parallel 1 --timeout 1800

#    方式 B：HuggingFace 拉取 + 固定种子（需 pip install datasets，首次下载数据集）
python -X utf8 scripts/run_swebench.py --hf swe-bench-lite `
    --max-instances 20 --seed 42 --save-subset data/swebench_lite_20.jsonl `
    --results-dir logs/swebench/run1
```

运行前确保：

- 已设置 `DEEPSEEK_API_KEY`（或用 `--config` 指向其他 LLM 配置）；
- 本机有 git 与网络（克隆 GitHub 仓库）；
- 机器内存/CPU 允许：`--max-parallel` 建议 1-2。

## 3. 输出产物

每次运行在 `logs/swebench/<run>/` 下：

```
<instance_id>/
  problem_statement.txt   # 发给 Agent 的 issue 文本
  patch.diff              # Agent 生成的 patch
  adapter.json            # Agent 运行指标（token/轮次/耗时/文件）
  eval.json               # 评估结果（逐测试状态）
  result.json             # 汇总结果
results.jsonl             # 全部结果流式追加（崩溃不丢）
summary.json              # 聚合指标
report.md                 # Markdown 报告（状态分布 + 失败分类 + 明细）
```

## 4. 指标口径

- **resolved**：Agent patch 在干净快照 + test_patch 下，`FAIL_TO_PASS` 全过且 `PASS_TO_PASS` 全过；
- **unresolved**：patch 可应用但测试未全过；
- **error / timeout / budget**：Agent 阶段失败；
- 辅助指标：解决率、平均 token、平均轮次、平均耗时、状态分布、失败归因分类。

> 官方口径：安装 `swebench` 包后可调用 `swe_eval.evaluate.official_eval_config()`
> 桥接官方测试环境，得到与社区可比的分数。本地回退口径用于快速迭代，二者对比时注意标注。

## 5. 失败归因工作流（阶段三）

```powershell
python -X utf8 -c "import json, pathlib; from swe_eval.analyze import *; ..."
```

或在 `report.md` 中直接查看分类。归因类别来自 Agent 输出的
`attribution.category`（`agent/attribution.py`），可配合
`logs/sessions/` 会话档案回放具体决策轨迹。

高频失败模式排查建议：

- **检索失败**：确认 `config/swebench.yaml` 的 `workspace_context_enabled: true`，
  仓库结构摘要已注入 Prompt（`prompt_builder.set_project_profile`）；
- **修改失败（测试未过）**：检查 `eval.json` 中具体测试输出，判断是修错逻辑还是没跑测试；
- **上下文失败**：调大 `context.max_tokens` 或延迟压缩阈值；
- **工具超时**：调大 `tools.terminal_execute.timeout`，或在提示中要求只跑相关测试。

## 6. 持续评估（阶段五建议）

- 把 `--save-subset` 固化的 20-30 例子集放入仓库（`data/swebench/`），
  每次核心改动后跑 `scripts/run_swebench.py --instances data/swebench/... --no-eval`；
- 将 `summary.json` 结果按时间归档，形成能力退化看板；
- 对比 A/B：同一子集分别用 `config/default.yaml` 与优化分支配置运行，比较解决率。

## 7. 已知边界

- 本地回退评估假设仓库测试可直接用系统 Python 运行；复杂环境（需要特定
  Python 版本/系统依赖）建议切换到官方 swebench Docker 环境；
- `git worktree` 在本仓库 Windows/沙箱环境不可靠，`evaluate.py` 改用
  `git archive` 快照，已覆盖绝大多数场景；
- SWE-bench 全量 Lite（300 例）需要较多时间与 token，建议先跑 20-30 例子集。
