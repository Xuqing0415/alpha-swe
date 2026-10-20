# 配置系统

> 本文档从根 README 迁移「配置影响验证」。配置通过 `agent/config.py`（Pydantic + YAML）三层降级加载：用户指定路径 → 项目根目录 → 内置硬编码默认。
>
> 配置文件：`config/agent.yaml`（Agent 全局配置）、`config/mcp.yaml`（MCP 服务器清单）、`config/default.yaml`（基线配置）、`config/aggressive.yaml`（A/B 对比激进配置）、`config/offline.yaml`（完全离线演示）。

## 配置影响验证（配置 → 运行时数据流）

每个配置键在运行时决策点被读取并记录到 `decision_log.jsonl`（可用 `DECISION_LOG_PATH` 覆盖路径）。
运行对比测试可确认配置是否真实改变 Agent 行为：

```powershell
# 1) A/B 对比测试（default.yaml vs aggressive.yaml，断言决策日志差异）
python -X utf8 -m pytest tests/test_config_impact.py -q

# 2) 分析决策日志：列出已生效/未生效的配置项
python -X utf8 scripts/analyze_decisions.py
```

决策点覆盖：`llm.provider`（系统提示风格）、`llm.temperature`（解析器宽松度）、
`sandbox.network_enabled`（容器网络模式 / 网络命令拦截）、`context.max_tokens` 与 `compression_threshold`（压缩触发）、
`context.compression_method`（summary / vector_retrieval）、`memory.backend`（记忆后端 / 检索跳过）、
`planner.*`（拆分阈值 / 子任务上限 / 串并行）、`agent.*`（循环上限 / 工具并行 / 确认与自动批准、
追踪/档案/指标开关 `trace_enabled` / `archive_enabled` / `metrics_enabled`）。


相关文档：[architecture.md](architecture.md)（架构）、[status.md](status.md)（验证记录）。

## 目标驱动、能力画像与增量快照

以下键位于 `config/agent.yaml` 的 `agent` 段。默认值以 `agent/config.py` 为准；
未知键或非法类型按配置系统的三层降级规则回退。

| 配置键 | 默认 | 作用 |
|--------|------|------|
| `goal_enabled` | `false` | 加载目标台账并在 System Prompt 中注入活跃目标 |
| `goal_state_path` | `.swe-agent/goals.json` | 目标台账路径（相对工作区） |
| `goal_idle_continue_enabled` | `true` | 队列清空但目标未完成时触发续规划 |
| `goal_idle_continue_limit` | `3` | 单次运行最多续规划轮数，防止空转 |
| `difficulty_critic_enabled` | `true` | 任务结束后按实际过程评估难度，写入能力画像 |
| `benchmark_duplicate_threshold` | `0.85` | 基准集语义去重阈值 |
| `benchmark_variant_threshold` | `0.6` | 相关但不同的任务标记为变体 |
| `session_distill_enabled` | `false` | 会话结束把有跨会话价值的状态提炼为项目记忆 |
| `state_tracker_enabled` | `true` | 项目快照；默认只哈希 Agent 写入文件与关键配置 |

大项目快照首次建立基线，此后按 `mtime/size` 检测普通文件、按内容哈希检测
Agent 写入文件与关键配置；`scan(full_hash=True)` 仅用于人工全量校验。

