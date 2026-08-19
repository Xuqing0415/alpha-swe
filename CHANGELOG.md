# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-08-19

### 修复
- TUI：修复 `tui/app.py` 中 `Dict`/`LogMessage` 未定义、残留引用未定义 `lines` 的死代码、
  未使用变量等 NameError 隐患；`tui/logbridge.py` 修复未定义 `logger` 导致的异常路径崩溃。
- Planner：移除 `_parse_plan` 从未使用的 `fallback_prompt` 参数。
- 旧版七层原型：清理 f-string 缺失占位符（F541）与未使用循环变量（B007）。
- CI：`chaos.yml` 补齐 `mcp`/`scikit-learn` 依赖（修复 chaos-stage 探针批量失败）；
  `chaos.yml`/`quality-gate.yml` 增加 `concurrency` 取消旧 run 与最小权限 `permissions`。

### 新增
- 测试：新增 `tests/test_database_cloud_tools.py` 与 `tests/test_memory_edge.py`（45 例），
  覆盖数据库工具安全策略、云 CLI 超时/降级、记忆后端边界路径，核心模块覆盖率提升至约 85%。
- 配置：新增 `config/minimal.yaml` 最小配置样例（配合完整样例 `config/agent.yaml`）。

### 质量
- 全仓 flake8（F/B/C4/E9）零告警；vulture 死代码扫描仅剩上下文管理器协议必需参数（误报）。
- `pip-audit` 依赖漏洞扫描：运行时与测试依赖均无已知漏洞。
## [0.1.0] - 2026-08-18

### 新增
- 异步 Agent 核心：DAG 任务调度、状态机、决策日志、上下文压缩、技能/插件注入。
- 代码语义理解：Python/JS/TS AST 摘要、调用图、项目画像、issue→文件推荐。
- 安全沙箱：路径隔离、危险命令拦截、网络策略（deny/allowlist/allow）、Docker 预留。
- 长期记忆：混合检索（TF-IDF/向量）、经验/错误记忆、去重与衰减。
- 产品化服务：FastAPI 任务 API、用户/权限、审计日志、SSE 事件流。
- SWE-bench 评估：数据集加载、Agent 适配器、评估器、批量运行、失败归因、实验日志。
- 多语言基础设施（v0.1 首发）：Java/Go/Rust/C/C++/C#/Ruby/PHP 符号与调用图提取、
  测试运行器扩展（Maven/Gradle/go/cargo/CTest）、依赖清单识别。
- 扩展工具：数据库查询（SQLite/PostgreSQL/MySQL）、依赖审计、云 CLI 封装（默认关闭）。
- VS Code 扩展 MVP：选中代码 + 自然语言指令，调用 Agent 服务端任务 API。

### 说明
- 0.1.0 为内部基线版本；公开发布与社区运营待后续进行。
