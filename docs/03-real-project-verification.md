# 方向一 · 阶段 3：真实项目长期任务实战报告

- 验证日期：2026-08-18
- 基准项目：`tests/benchmarks/sample_project`（taskboard：Flask 风格任务看板，21 项 pytest）
- 执行器：`scripts/run_real_project_tasks.py`（复制项目→真实 LLM 执行→完成标准校验→汇总报告）
- 基准配置：`config/benchmark.yaml`（litellm + deepseek-chat，`--timeout 600`，`--max-cost 1.0/任务`）
- 任务集：8 个（L1×2 / L2×2 / L3×2 / L4×2），覆盖变量改名、docstring 修正、bug 修复+回归测试、
  参数校验、跨文件重构、新增 CLI 子命令、新增 API+测试

## 一、实战结果

- 运行方式：`scripts/run_real_project_tasks.py`，真实 DeepSeek-chat（litellm），
  每任务独立子进程 + 独立工作区副本，`--timeout 600`、`--max-cost 1.0`。
- 判定口径：
  - **完成标准（端态）**：执行器按任务 `check`（pytest 全绿 + 内容断言）对最终工作区判定；
  - **Agent 自报**：子进程 `exit_code==0`。

| 任务 | 难度 | 端态通过 | Agent 自报 | token（该任务最后一次跑） | 失败/备注 |
|---|---|---|---|---|---|
| L1-01 移除未用 import + 改名 | L1 | ✅ | ✅ | 84,290 | — |
| L1-02 修正 docstring 单位 | L1 | ✅ | ✅ | 125,228 | — |
| L2-03 search 大小写不敏感 + 测试 | L2 | ✅ | ✅ | 152,937 | 用 `.casefold()` 实现，检查器已兼容 |
| L2-04 add() 参数校验 + 测试 | L2 | ❌ | ❌ | 504,563 | 编辑破坏语法/结构，pytest 未通过 |
| L3-05 Task.touch() 跨文件重构 | L3 | ❌ | ❌ | 417,305 | 编辑后 pytest 未通过 |
| L3-06 filter_by 委托 filter_tasks | L3 | ✅ | ❌ | 231,078 | 端态正确（pytest 全绿）但子任务自报失败 |
| L4-07 新增 CLI stats 子命令 + 测试 | L4 | ❌ | ❌ | 173,533 | 子命令已加，测试未补 |
| L4-08 新增 find_by_tags API + 测试 | L4 | ❌ | ❌ | 338,575 | 编辑后 pytest 未通过 |

### 汇总（端态口径）

| 指标 | 目标 | 实测 |
|---|---|---|
| 完成率 L1+L2 | > 80% | 75%（3/4） |
| 完成率 L3 | > 50% | 50%（1/2） |
| 完成率 L4 | > 30% | 0%（0/2） |
| 总体完成率 | — | 50%（4/8） |
| 平均 token/任务 | 记录基线 | ≈ 253k |
| 平均耗时/任务 | — | ≈ 1.5-2.5 min（token 上限内） |
| 回归率（端态 pytest 被改坏） | < 15% | 37.5%（3/8 端态 pytest 失败） |

> 说明：L2-04/L3-05/L4-08 的端态 pytest 失败是「编辑损坏」而非用例失败——模型用
> 行号区间编辑时重复/吞并相邻代码（见问题 6），已通过即时语法校验缓解；DeepSeek-chat
> 的编辑精度与收敛效率是当前主要瓶颈。

## 二、验证过程中发现并修复的系统问题

### 1. 调度器死锁：前置任务失败后依赖者永久 WAITING（严重，已修复）

- **现象**：真实 LLM 运行中，任一子任务失败（如 token 预算耗尽）后，依赖它的后续任务
  保持 WAITING 永不终结——`run_to_completion` 每 30s 打出「等待唤醒超时，重新检查就绪任务」
  空转，直到外层超时强杀（`rounds=0` 是调度循环未返回的次生现象）。
- **根因**：`TaskDAG.promote_dependents` 只处理「依赖已 COMPLETED/SKIPPED」的情况；
  依赖 FAILED 的任务既不被提升也不被终结，形成调度死等。
- **修复**（`agent/core/task.py` + `agent/core/scheduler.py`）：
  - 新增 `abort_dependents()`：失败后级联终止依赖者——critical 依赖者标 FAILED
    （失败向上传播），normal/optional 标 SKIPPED（不阻塞其它分支）；
  - `on_task_done` 在任务 FAILED 时主动调用；`run_to_completion` 每轮做
    `abort_failed_dependencies()` 防御性兜底（覆盖快照恢复 / spawn 产生的异常状态）。
- **回归**：`tests/test_scheduler.py` 新增 2 例（关键依赖级联失败不挂起 / 非关键依赖跳过不阻塞）。

### 2. 子任务 token 预算按整轮上下文计费，简单任务 3-8 轮即耗尽（已修复）

- **现象**：每个 ReAct 轮次 `estimate_tokens(messages)` 统计的是「系统提示 + 注入的项目上下文 +
  Tool schema + 历史」的完整消息，一次工具调用约 3-4k token。默认 `budget_token_base=10000`
  时，简单「读文件」子任务 8 轮（~31k token）即触发预算耗尽 → 任务 FAILED。
- **修复**（`agent/config.py`）：`budget_token_base / default_token_budget` 10000→100000，
  `budget_time_base / default_time_budget` 300→1800s，使预算与「30 轮 × 每轮 ~4k」的真实成本对齐，
  同时保留防失控兜底。

### 3. 基准执行器 JSON 解析失效（已修复）

- **现象**：CLI `--output json` 输出多行美化 JSON，执行器用 `raw.splitlines()[-1]`（仅最后一行 `}`）
  解析，导致所有任务的 rounds/tokens/error 全部丢失，报告只剩 pass/exit。
- **修复**（`scripts/run_real_project_tasks.py`）：先整体 `json.loads(raw)`，失败则从第一个 `{` 截取。

### 4. 模型重复调用相同工具导致无进展空转（已修复）

- **现象**：会话档案显示模型对同一文件 13+ 次 `file_ops read`、10 次 `git log`（相同
  tool+params），从不输出 `final_answer`，白白烧预算；预算兜底能终止但任务失败。
- **修复**（`agent/core/loop.py` + `agent/config.py`）：新增窗口化重复工具调用保护
  `tool_repeat_limit=5` / `tool_repeat_window=15`——相同 tool+params 在最近 15 轮内出现
  达 5 次即拦截执行并注入纠偏提示（「结果不会变化，请基于已有信息继续或输出 final_answer」）；
  单任务累计拦截 4 次仍无收敛则终止任务（防无限空转）。决策日志记 `tool.repeat_guard`。
  第一版仅检测「连续重复」，模型通过交替调用其它工具即可绕过，故改为窗口统计。
- **回归**：`tests/test_config_impact.py::test_repeat_tool_call_guard`。

### 5. 工具输出截断过严，模型被迫反复部分读取文件（已修复）

- **现象**：`context.output_truncate=2000` 时，4KB 的 `board.py` 被截成「头 500 + 尾 500 +
  关键行」，模型看不到文件中间部分，只能靠大量 `start_line/end_line` 分片读取拼出全貌，
  单子任务因此消耗 28-30 轮 / 12 万+ token。
- **修复**（`agent/config.py` + `config/benchmark.yaml`）：`output_truncate` 2000→8000，
  允许完整显示中小文件（≈2k token/次），显著减少分片读取。

### 6. 模型的按行编辑破坏文件结构（已加即时校验）

- **现象**：模型用 `file_ops edit`（行区间替换）时行号计算错误，导致 `def get` 被复制 3 次、
  `def update` 被吞掉——pytest 直接语法错误，而模型还在继续无意义地重试。
- **修复**（`agent/core/loop.py` + `agent/config.py`）：新增 `syntax_check_enabled`（默认开）——
  每次 `write/edit/append` 一个 `.py` 文件后立即 `ast.parse` 校验；语法错误即刻以
  「[语法校验] ... 行 N 列 M」返回失败，模型下一轮就能定位并修复，避免在坏文件上继续空转。
- **回归**：`tests/test_config_impact.py::test_syntax_check_after_broken_write`。

## 三、失败归因（按会话档案分析）

| 模式 | 出现任务 | 根因 | 已采取措施 |
|---|---|---|---|
| 子任务 token/轮次预算耗尽 | L3-06/L4-07/L4-08（首轮） | 每轮按完整上下文计费，模型 28-30 轮读完一个 4KB 文件 | 预算 10k→100k；output_truncate 2000→8000 |
| 重复探索（同文件反复 read / git log） | L2-04/L3-05 等 | 模型无收敛地交替调用工具 | 窗口化重复调用拦截（5 次/15 轮）+ 4 次拦截后终止 |
| 编辑破坏文件结构 | L2-04/L3-05/L4-08 | 行号区间编辑把 `def` 复制/吞并 | 写后即时 `ast.parse` 语法校验并回写错误 |
| 端态正确但自报失败 | L3-06 | 完成修改后，收尾子任务未输出 final_answer / 预算耗尽 | 建议评估按端态口径 + 压缩收尾子任务上下文 |
| 预算上限被触发（$0.5） | L2-03/04、L3-05（早期跑） | 模型单任务消耗 25 万+ token | 上限提至 $1.0；DeepSeek 实际成本约 $0.2-0.3/任务 |

## 四、会话连续性 / 断点续跑

- 快照路径与 `resume` 已由既有 `tests/test_snapshot_resume.py` / `test_workspace_context.py` 覆盖；
  本阶段真实 LLM 任务均为独立子进程，单任务一次会话完成，未引入新的会话依赖。
- 断点续跑依赖 `snapshot_enabled`（benchmark 中为 false 以控制磁盘）；生产建议开启，
  详见 `docs/04-multi-instance-guide.md`。

## 五、改进点提炼（提交为后续迭代待办）

1. **编辑工具升级**：行号区间编辑对模型精度要求过高，建议增加「锚点字符串替换
   （old_string → new_string）+ 唯一性校验」模式（SWE-bench 惯例），从源头减少编辑损坏。
2. **上下文压缩策略**：压缩时保留最近 1-2 次工具输出原文，减少模型「忘了内容→重复读取」。
3. **评估口径**：长期任务建议以「端态 + 测试」为准（本阶段 L3-06 即端态正确但自报失败），
   同时把「Agent 自报 vs 端态」的差异纳入回归监控。
4. **模型选型**：DeepSeek-chat 在 ReAct 循环中效率偏低（简单任务 15-50 万 token）；
   生产环境建议评估更强/更省 token 的模型，或启用任务级缓存与更激进的压缩。

## 六、如何复跑

```bash
# 仅校验完成标准（不消耗 LLM，CI 用）
python -X utf8 scripts/run_real_project_tasks.py --skip-llm

# 真实 LLM 全量跑（8 任务）
python -X utf8 scripts/run_real_project_tasks.py --tasks L1-01,L1-02,L2-03,L2-04,L3-05,L3-06,L4-07,L4-08 --timeout 600 --max-cost 0.5

# 单任务
python -X utf8 scripts/run_real_project_tasks.py --tasks L2-03 --timeout 600 --max-cost 0.5
```

报告输出：`logs/real_project_report.json`（含逐任务 pass/耗时/token/错误/归因）。
