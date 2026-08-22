# 项目现状与验证

> 本文档从根 README 迁移，保留 2026-08 审计结果与真实项目验证记录。
> 历史报告：`01-docker-verification.md`、`02-concurrency-verification.md`、`03-real-project-verification.md`、`04-multi-instance-guide.md`、`05-swebench-benchmark.md`、`07-code-quality.md`。

## 项目现状核对（2026-08 审计）

对照全量复盘清单逐项核对后的更正（详见 `../logs/FIX_REPORT.md` 与历次提交）：

- **测试总量**：`tests/` 全量 569 passed / 0 failed；旧原型 `test_all.py` 13 passed / 0 failed（清单的「351+」
  已过时）。
- **基准集路径**：清单写的 `tests/benchmarks/` 不存在；实际在 `tests/test_benchmark_suite.py`（28 例）+
  `tests/test_real_project_suite.py`（18 例）+ `tests/test_long_task_suite.py`（4 例），共 50 例。
- **长时间浸泡测试**：已实现（`tests/test_soak.py`：24 会话顺序流 + 3×8 并发流，内存/句柄/事件列表有界），
  不再属于未完成项。
- **真实 LLM 端到端**：已用 litellm + DeepSeek（真实 `DEEPSEEK_API_KEY`）实测通过（`python -m agent run`），
  不再是「未验证」；缺 Key 时 CLI 快速失败并给出清晰报错。
- **技能数量**：`skills/skill_manifest.json` 注册 19 个技能定义（3 个 Markdown + 15 个工作流 YAML），
  清单「内置 10 个技能」已过时。
- **真实 Docker 容器沙箱**：已在 Docker Desktop 29.6.1 下真实验证（18 项真实 daemon 用例 + 13 项 fake 离线用例全部通过）；
  报告见 `01-docker-verification.md`，修复了 exec 无 shell 语法/镜像 CMD/tmpfs 格式/路径穿越/超时恢复/快照清理等 6 个真实问题。
- **多用户/多实例并发**：已验证（项目锁/文件写锁/SQLite WAL 共享记忆、9 项跨进程测试全部通过）；
  报告见 `02-concurrency-verification.md`，部署指南见 `04-multi-instance-guide.md`。
- **真实项目长期任务实战**：已在基准项目 `tests/benchmarks/sample_project` 上执行 L1-L4 真实 LLM 任务（报告见 `03-real-project-verification.md`）；
  运行器 `scripts/run_real_project_tasks.py`、基准配置 `config/benchmark.yaml`。
- **>8h 连续运行稳定性**：仍未长期现场打卡（保留作为后续项）。


## 真实项目基准实战（方向一 · 阶段 3）

- **基准项目**：`tests/benchmarks/sample_project`（taskboard，21 项 pytest），植入 8 个
  L1-L4 缺陷/改进点；完成标准用「pytest 全绿 + 内容断言」自动判定。
- **运行器**：`scripts/run_real_project_tasks.py`（复制项目→真实 LLM 独立子进程执行→
  端态校验→汇总 `../logs/real_project_report.json`）；`--skip-llm` 只校验完成标准（CI 回归用）。
- **实测（DeepSeek-chat，2026-08-18）**：端态完成率 4/8（L1 2/2、L2 1/2、L3 1/2、L4 0/2），
  平均 ≈25 万 token/任务。发现并修复：调度器失败依赖死锁、子任务预算过紧、工具输出截断过严、
  模型重复工具调用空转、写坏 `.py` 无即时反馈（详见 `docs/03-real-project-verification.md`）。


## 真实项目持续工作（主线一 1.1/1.2）

- **项目状态感知**（`agent/project_state.py`）：`.swe-agent/state.json` 持久化项目结构快照、
  依赖清单与技术栈、依赖变更历史、最近修改记录、测试健康与技术债标记；会话启动对比上次快照，
  把「上次会话以来的项目变化」（含依赖升级 breaking changes 提示）注入 Prompt。
- **会话间工作流连续性**（`agent/workspace_context.py`）：`.swe-agent/context.json` 记录
  active_branch / current_task_id / task_phase / pending_actions / uncommitted_changes /
  next_session_hint；会话结束自动生成续接提示，新会话检测到未完成任务时在 TUI 显示
  「上次你在做 X，处于 Y 阶段，建议继续 Z」并注入初始 Prompt。
- 配置开关：`agent.state_tracker_enabled` / `agent.workspace_context_enabled`（默认开启）；
  `.swe-agent/` 已加入 `.gitignore`。

