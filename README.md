# Alpha-SWE Agent

基于设计文档落地的最小但可扩展的 SWE Agent 系统。核心通信完全异步（asyncio），
任务以 DAG 调度，支持长期记忆、技能注入、上下文压缩、安全沙箱与用户中断。

## 目录结构

```
agent/                  新核心架构（本设计的主干）
├── core/               异步主循环、Task/TaskDAG、状态机、调度器
├── decision_logger.py  决策点日志（config_key -> 决策，JSONL）
├── planner/            任务拆分（LLM，失败回退单任务）
├── prompt/             Jinja2 Prompt 构建（系统提示 + 工具 Schema + 记忆/技能）
├── parser/             输出解析（JSON 代码块 / 文本回退 / 重试反馈）
├── tools/              统一 Tool 接口 + Terminal / FileIO / 注册管理器
├── memory/             长期记忆（向量检索 + 经验/代码/错误记忆，多后端可插拔）
│   ├── store.py        存储后端：Hybrid / Chroma / Qdrant / SQLite
│   ├── embed.py        嵌入器：TF-IDF / sentence-transformers / OpenAI API
│   ├── summarizer.py   经验摘要生成（LLM，失败回退规则提取）
│   └── factory.py      按配置选择后端（auto 自动探测）
├── context/            技能/插件激活 + 上下文自动压缩
├── sandbox/            路径/命令安全策略（Docker 沙箱预留）
├── docker_sandbox.py  Docker 容器 spec 生成（SandboxConfig 驱动）
├── config.py           Pydantic + YAML 配置
└── llm.py              模型接口（mock / litellm）
config/
├── agent.yaml          Agent 全局配置
├── mcp.yaml            MCP 服务器清单
├── default.yaml       基线配置（可复制为自定义配置）
└── aggressive.yaml     A/B 对比激进配置（17 项关键参数取反）
scripts/analyze_decisions.py  决策日志分析：列出已生效/未生效配置项
examples/quick_demo.py  脚本化 LLM 的端到端演示
tests/                  新核心测试（111 项）
loop.py, scheduler.py, ... 旧版七层原型（保留，作为对照参考）
```

## 设计与实现的对应关系

| 设计章节 | 实现位置 | 状态 |
| --- | --- | --- |
| 2 主循环与状态机 | `agent/core/state.py`、`agent/core/loop.py` | 已实现（IDLE→PLANNING→READY→RUNNING→WAITING→COMPLETED/FAILED） |
| 2.2 中断与优先级 | `AgentLoop.interrupt()` + 调度器高优先级任务 | 已实现 |
| 3 任务调度与拆分 | `agent/core/task.py`、`agent/core/scheduler.py`、`agent/planner/` | 已实现（依赖、优先级、并发、spawn） |
| 4 Prompt 动态拼接 | `agent/prompt/builder.py` | 已实现（Jinja2、工具 Schema 动态化） |
| 5 输出解析 | `agent/parser/parser.py` | 已实现（重试反馈、失败记录） |
| 6 Executor 与工具 | `agent/tools/` | 已实现（异步 Terminal/FileIO，超时终止） |
| 7 长期记忆 | `agent/memory/` | 已实现闭环（写入去重 + 反例降权 + 任务类型过滤 + 可信度衰减 + 引用计数，见 `test_memory_closed_loop.py` 的 A/A' 复用验证；Chroma/Qdrant/Hybrid/SQLite 可插拔） |
| 10 技能/插件 | `agent/context/manager.py` | 基础版（关键词激活；可扩展文件类型匹配） |
| 11 上下文压缩 | `agent/context/manager.py` | 已实现分级（light 压工具输出/medium 保留决策点/heavy 递归摘要），长输出关键行提取 + 原始存档引用，压缩决策日志（级别/前后 token/丢弃消息 ID），见 `test_compression_quality.py` |
| 12 沙箱 | `agent/sandbox/policy.py` + `agent/sandbox/audit.py` + `agent/sandbox/docker_sandbox.py` | 已实现（阶段五）：路径锚定/危险命令拦截；网络细粒度策略 deny\|allowlist\|allow + 假网络 + 请求审计；受保护路径防删/防写；文件操作审计与回滚；资源熔断。容器生命周期（阶段五后）：docker-py 惰性加载、start/exec_run/文件读写/快照 commit/回滚重建/超时 kill/stats，docker_enabled 时工具路由进容器 |
| 13 MCP | `agent/mcp/`（client/manager/tool）+ `config/mcp.yaml` + `mcp-servers/` | 已实现（阶段六）：stdio/sse/streamable-http 客户端、握手、工具合并、资源注入 Prompt；断连自动重连与降级隐藏（ensure_connected/retry_connect）；资源缓存 TTL；自研 TS 服务器（knowledge-base/issue-tracker） |
| 14 可观测性 | `tui/`（Textual 三栏）+ `AgentLoop.subscribe()` + 终端实时输出回调 | 已实现（思维流/终端流/状态栏/Ctrl+I 中断/Ctrl+P 暂停）；Web 面板与 OTel 导出待接入 |
| 配置→运行时数据流 | `agent/config.py` + `agent/core/decision_logger.py` + `agent/sandbox/docker_sandbox.py` + `scripts/analyze_decisions.py` | 已实现：YAML→Pydantic→组件工厂→运行时决策点；JSONL 决策日志；default/aggressive A/B 对比测试 |

## 长期记忆（第 7 节）

- **写入**：任务完成后 LLM 自动生成经验摘要（problem/steps/solution/outcome/key_files），
  失败时记录错误记忆（错误类型 + 上下文），文件写入/读取后自动索引代码（路径 + 符号 + 片段）。
- **检索**：任务指令触发混合检索（向量相似度 + 关键词打分，`hybrid_weight_vector` 调权重），
  优先按任务类型（fix/add/refactor/test）过滤同类型经验，命中为空再放宽全量；
  结果注入 Prompt 的「检索到的历史记忆」区块。
- **闭环**：
  - 去重：写入前 `find_similar` 检查，相似度 ≥ `memory.dedup_threshold`（默认 0.95）只更新引用计数（`memory.dedup`）；
  - 反例：错误记忆标记 `negative=true`，检索时正例优先、反例降权（`counter_example_penalty`）；
  - 衰减：超过 `memory.decay_days` 未引用，分数按 `decay_factor` 指数衰减；被引用次数越多越可信（use_count 加成）；
  - 决策日志：`memory.write` / `memory.dedup` / `memory.retrieve` / `retrieval_skip` 记录写入与检索路径。
- **后端**：`config/agent.yaml` 的 `memory.backend`：
  - `auto`：有 `chromadb` 用之，否则有 `qdrant-client` 用之，否则本地 `hybrid`（TF-IDF，零新依赖）；
  - `sqlite`：纯关键词（最轻）；`hybrid`：本地向量 + 关键词；`chroma` / `qdrant`：真实向量库。
- **嵌入器**：`memory.embedder` 支持 `tfidf`（默认）、`sentence-transformers`（本地模型）、
  `openai`（Embeddings API，`embedding_api_key_env` 指定密钥环境变量）。

## 代码语义理解层（阶段一）

让 Agent 拥有程序员级别的代码感知能力，入口 `agent/code/`：

- **AST 感知读取**（`ast_summary.py`）：`file_ops` 读取代码文件时除原文外附加结构摘要——类/函数/方法签名、
  参数、装饰器、导入与导出符号；Python 用标准库 `ast`，JS/TS 优先 tree-sitter 精确提取，
  未安装时回退正则近似（决策日志 `symbol.retrieved`）。
- **调用图**（`call_graph.py`）：项目启动时构建 `caller -> callee` 有向边（Python `ast`、JS/TS tree-sitter），
  修改符号前可查"谁调用了它 / 它调用了谁"（`callers_of` / `callees_of`），
  FileIO 读取时注入影响范围，决策日志 `call_graph.hit` / `call_graph.indexed`。
- **项目约定提取**（`project_profile.py`）：扫描 `package.json` / `pyproject.toml` / `tsconfig.json` 等，
  自动识别技术栈（含版本）、lint/严格模式约定、顶层目录结构，作为持久化上下文注入 System Prompt
  （决策日志 `profile.injected`）。
- **Planner 注入**：`planner.plan(..., call_graph, project_context)` 把高频符号影响面与项目约定
  注入拆分提示，让子任务拆分考虑连带修改（决策日志 `planner.call_graph.injected`）。

安装 tree-sitter（可选，未安装自动回退正则）：
```powershell
pip install tree-sitter tree-sitter-javascript tree-sitter-typescript
```

验证：`tests/test_code_semantics.py`（AST 摘要 / 调用图 / 项目约定 / 工具与 Prompt 注入 / tree-sitter 与回退）。

## 技能与插件（第 10 节）

### 插件动态注入（`agent/context/plugin.py`）
插件 = 纯上下文注入（Markdown + YAML front-matter），目录 `config/agent.yaml` 的 `plugin.dir`（默认 `./plugins/`），
按文件 mtime 热加载（新增/修改无需重启）。激活条件可组合叠加，命中任意一类即激活：
- `keywords`：任务指令关键词（如「数据库」→ `sql` 插件）；
- `file_ext`：项目/任务涉及文件扩展名（如 `.tsx` → `react-ts` 插件）；
- `project_file`：项目文件路径模式（fnmatch，如 `**/package.json`）；
- `project_dep`：项目依赖名（自动解析 `package.json` / `requirements.txt` / `pyproject.toml`）。

多插件同时命中时按 `priority` 降序注入，超出 `plugin.max_active` 截断（决策日志 `plugin.activate` / `plugin.truncate`）；
`config.active_plugins` 非空时作为白名单过滤。工作区扫描与指令路径提取由 `ProjectContext` 提供。

### 技能工作流（`agent/context/skill.py`）
技能 = 预定义子任务序列（YAML，目录 `config/agent.yaml` 的 `skills.dir`，默认 `./skills/workflows/`），
热加载；`skills.enabled` 由 `config/agent.yaml` 显式开启（程序化 `AppConfig()` 默认关闭，Worker Agent 恒关闭）。

```yaml
name: add-rest-endpoint
triggers:
  keywords: [rest, api, 端点, endpoint]
  file_ext: [.py, .ts]
steps:
  - name: route          # 展开为子任务：定义 REST 路由
    instruction: 定义 REST 端点路由
  - name: validation
    instruction: 编写请求参数校验
    dependencies: [route]
    on_failure: fallback     # 步骤失败决策点：fallback / abort / orchestrate
    fallback: 改用最简参数校验并重试
```

- **技能注册表（阶段二 2.1）**：每个技能可声明 `requires`（依赖技能）、`permissions`（所需工具权限）、
  `params`（参数定义）、`version` / `tags` / `author`；`skills/skill_manifest.json` 注册表按技能名补缺省元数据
  （YAML 显式键优先）；`validate()` 校验步骤依赖/`on_failure`/重复名/`when` 条件键；`discover()` 返回命中 +
  requires 依赖闭包 + 上下文建议；`record_usage()` / `usage_summary()` 记录使用与成败历史（版本管理，写入
  `skills.usage_log`）。决策日志：`skill.registry.loaded` / `skill.registry.invalid` / `skill.discovered`；
  展开时 `Task.metadata` 携带 `skill_version` / `requires` / `permissions` / `step_index` / `step_total`。
- **技能意图过滤（真实项目防误触发）**：`skills.require_task_intent: true`（默认）时，工作流激活要求任务指令命中
  `keywords` / `file_ext`；`project_dep` / `project_file` 单独命中仅在 `discover()` 中作为「上下文建议」返回，
  避免「把 README 翻译成英文」这类无关任务误展开 Express 工作流（决策日志 `skill.discovered` 区分两类命中）。
- 技能命中时由 `SkillLibrary.expand()` / `expand_pipeline()` 展开为 Task DAG（步骤依赖 -> 任务依赖），
  替代 LLM 规划器；多技能按给定顺序管道串联，前一个技能的最后一步链接到后一个技能的第一步（决策日志 `skill.pipeline`）；
- **步骤条件分支（阶段二 2.2）**：`SkillStep.when` 支持 `file_exists` / `not_file_exists` / `keyword` /
  `project_dep` / `always`（AND 语义），条件不满足的步骤跳过并记录 `skill.step_skip`；
  步骤决策点写入 `Task.metadata`：`on_failure=fallback` 时失败自动 `spawn` 回退任务（`skill.step_fallback`），
  `orchestrate` 时发出 `skill_intervention` 事件请求介入；
- 决策日志：`skill.activate` / `skill.expand` / `skill.pipeline` / `skill.step_skip` /
  `skill.step_fallback` / `skill.step_intervention`。

### 自然语言创建技能（阶段二 2.3，`agent/context/skill_author.py`）

- **确定性轨迹转换**：`SkillAuthor.from_trajectory()` 把已执行 Task 列表（含 `skill_step` 元数据）转换为技能
  YAML——步骤按顺序依赖，失败步骤自动标 `on_failure: fallback` 并带重试指令，触发器从技能名/描述自动提取关键词
  （含中文 2-3 字窗口）。
- **LLM 生成**：`SkillAuthor.from_llm()` 用 LLM 把自然语言/轨迹生成为更规范、带条件的技能 YAML（只接受
  ```yaml 代码块），失败自动回退确定性转换。
- **落盘热加载**：`SkillAuthor.save()` 写入技能库目录并立即用 `SkillLibrary` 校验，可被 `discover()` / `match()`
  马上发现（决策日志 `skill.authored` / `skill.saved`）。
- **入口**：`AgentLoop.save_skill(name, description)`（保存最近任务轨迹）与
  `AgentLoop.save_skill_from_natural_language(name, description, prompt)`（LLM 生成）；CLI 见 `scripts/save_skill.py`：
  ```powershell
  # trajectory.json: [{"step": "reproduce", "instruction": "复现问题", "outcome": "completed"}, ...]
  python -X utf8 scripts/save_skill.py --name fix-login-bug --description "修复登录失败" --trajectory trajectory.json
  python -X utf8 scripts/save_skill.py --name setup-env --description "初始化环境" --llm-prompt "把设置本地开发环境的步骤做成技能"
  ```

### 真实技能库与项目测试集（阶段二 2.4 验证）

- `skills/workflows/` 内置真实工作流：`add-rest-endpoint`、`bug-fix`（复现→定位→修复→回归）、
  `db-migration`（分析→生成→执行→回滚方案）、`test-generation`（分析→写用例→运行→修复失败）、
  `python-refactor`；`skills/skill_manifest.json` 注册表补齐 requires/permissions/params/tags。
- `tests/test_real_project_suite.py`（9 用例）：用 Express / Django+SQLAlchemy / pytest / Flask 迷你项目验证
  陌生项目上的技能发现、优先级排序、无关任务零误触发，以及 `AgentLoop` 端到端激活顺序与 `skills_activated` 事件。
- **技能执行进度可视化（阶段二 2.4）**：`task_start` 事件携带 `skill` / `skill_step` / `step_index` / `step_total`；
  TUI 任务树视图为技能步骤显示 `[技能 name::step i/N]` 徽标，底部状态栏与思维流同步显示当前技能进度
  （`skills_activated` 事件渲染为「技能工作流激活: ...（展开 N 个子任务）」）。

## 多 Agent 协作（第 8 节）

团队配置见 `config/team.yaml`（可插拔角色），入口 `agent/multiagent/`（Orchestrator/Worker + 黑板）。

- **角色权限**：`WorkerRoleConfig.read_only` 控制只读角色——`file_ops` 拒绝 write/append，`terminal_execute` 只放行
  只读命令白名单（`cat`/`ls`/`grep`/`git diff` 等，禁止管道/重定向/`;`/`&&` 等写语义元字符）。`config/team.yaml` 中
  reviewer 默认 `read_only: true`。
- **消息协议**：`Message{sender, receiver, type, payload, priority, timeout}`；Orchestrator 派发 TASK_ASSIGN 时携带
  任务优先级与 `team.message_timeout` 超时。
- **自动路由**：LLM 规划未给出角色或角色未配置时，按指令关键词分类回退（编码类→coder、审查类→reviewer、
  测试类→tester），决策日志记录 `role.routing`。
- **冲突仲裁**：Reviewer 返回 retry 时带反馈重建 coder 任务并复审（最多 `team.max_review_retries` 轮，计数锚定根
  coder）；耗尽后升级人工介入——`TeamResult.needs_intervention=True` + 决策日志 `review.exhausted`。
- **共享上下文**：Worker 产出（文件 diff、测试报告）发布到黑板，下游 Reviewer/Tester 只读挂载上游产物。

## 沙箱安全加固（阶段五）

入口 `agent/sandbox/policy.py`（策略）+ `agent/sandbox/audit.py`（审计），由 `config/agent.yaml` 的 `sandbox` 段驱动。

- **网络细粒度策略**：`network_policy` 支持 `deny`（默认，拦截所有网络命令）/ `allowlist`（仅放行
  `network_allowed_commands`，如 `pip install`、`apt-get`）/ `allow`（全局放行）；决策日志记录 `block_network_command`
  与 `network.audit`（自动提取 curl/wget/git clone 的目标 URL）。
- **假网络模式**：`fake_network: true` + `fake_network_responses`（URL 前缀 → 响应体）时，`curl`/`wget` 命中预设
  即返回响应文本、不发起真实请求（决策日志 `network.fake`），用于离线/受控测试。
- **文件系统保护**：`protected_paths`（默认 `.git`、`config/*.yaml`、`*.lock`）拦截受保护路径的写入与删除
  （`rm`/`del`/`Remove-Item`），并新增路径 NUL 字节检查（决策日志 `file.protect`）。
- **操作审计与回滚**：`FileAuditStore` 记录每次 write/append 的 before/after 内容与 unified diff 到
  `audit_dir/file_audit.jsonl`；`rollback(path)` 用最近一条审计的 before 内容恢复文件。
- **资源监控与熔断**：`resource_monitor: true` 时 Terminal 周期采样进程树 RSS（psutil），超过 `memory_limit_mb`
  立即 kill 整棵进程树并返回 `circuit_breaker=True` 的失败结果，Agent 可据此选择更低内存的方案重试。

验证见 `tests/test_sandbox_security.py`（危险命令、deny/allowlist/假网络、受保护路径、审计回滚、熔断、循环内恶意命令拦截端到端）。

## Docker 沙箱容器生命周期（第 12 节）

`agent/sandbox/docker_sandbox.py` 由 `sandbox.docker_enabled` 驱动（默认关闭，本地工具不变）。

- **生命周期**：`start()` 拉取镜像（缺失时 pull）→ 创建容器（卷挂载 `workdir=/workspace`、只读根、资源限制、网络隔离）→ 启动；
  `exec_run()` 容器内执行命令，超时强制 kill；`stop()/stats()/status()` 清理与资源采样。
- **网络隔离**：`no_network`（默认）→ `network_mode=none`；`network_enabled: true` → `bridge`。
- **资源限制**：`memory_limit` / `cpu_limit` 直接映射 docker `mem_limit` / `nano_cpus`；命令级超时由 `timeout_seconds` 控制。
- **快照/回滚**：每个任务执行前 `docker commit` 快照（`snapshot_prefix`）；任务失败且 `auto_rollback: true` 时
  `rollback()` 从快照镜像重建容器，失败后可快速回到干净状态。决策日志：`docker.start/snapshot/rollback/exec/timeout/stop`。
- **工具路由**：docker 模式下 `TerminalTool` 经 `exec_run` 执行，`FileIOTool` 经 get/put_archive 操作容器文件系统
  （路径仍先过本地沙箱策略）；容器启动失败自动降级回本地执行。

验证见 `tests/test_docker_sandbox.py`（fake docker client 离线覆盖规格/生命周期/文件/超时/快照回滚/主循环端到端，13 项）。

## 快速开始

```powershell
# 安装依赖（已安装则跳过）
pip install pyyaml pydantic jinja2 pytest pytest-asyncio litellm
# 可选：真实向量库后端
pip install qdrant-client    # 或 chromadb
# 可选：JS/TS 精确代码解析（未安装时回退正则）
pip install tree-sitter tree-sitter-javascript tree-sitter-typescript

# 运行测试（终端工具测试会真实创建子进程）
python -X utf8 -m pytest tests -q

# 最小演示：脚本化 LLM 驱动一次完整 ReAct（terminal -> final_answer）
python -X utf8 examples/quick_demo.py
```

## MCP 生态打通（阶段六）

- **动态发现与断连处理**：`MCPManager.ensure_connected()` 首次连接 + 按 `mcp.reconnect_attempts` 自动重连；
  失败服务器记录在 `failed_servers`，其工具从 `build_tools()` 降级隐藏（决策日志 `mcp.connect_failed/reconnect/degrade`）。
- **资源缓存**：`read_resource()` 带 TTL 缓存（`mcp.resource_cache_ttl`，0 = 不缓存），命中记录
  `mcp.resource_cache_hit`；`invalidate_resources()` 支持按服务器/URI 失效。
- **自定义 TypeScript 服务器**（`mcp-servers/`）：`knowledge-base`（团队知识库：`search_kb`/`add_kb_entry` +
  `kb://` 资源）、`issue-tracker`（Issue 追踪：`list_issues`/`create_issue`/`update_issue_status` + `issue://` 资源）。
  构建：`npm install && npm run build`，注册进 `config/mcp.yaml`（stdio）。

验证见 `tests/test_mcp_phase6.py`（重连/降级/缓存）与 `tests/test_mcp_ts_servers.py`（真实 TS 服务器端到端）。

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

## 可观测性与 TUI 交互升级（阶段七 / 阶段八）

### 分布式追踪（`agent/observability/trace.py`）
- OTel 风格 `Span` / `Tracer`：`run -> task -> llm/tool` 调用链（父子 span_id），
  结束后导出 JSONL 到 `agent.trace_dir`（默认 `./logs/traces`；`trace_enabled: false` 时零成本跳过）。
- 每次 `AgentLoop.run()` 生成 `run` 根 span；每个任务、每次 LLM 调用、每个工具调用各生成一个 span，
  记录耗时、状态、错误与关键属性（token、轮次、参数摘要）。

### 实时指标（`agent/observability/metrics.py`）
- `MetricsRegistry` 计数/采样：token 用量与速率、工具调用与成功率、LLM 调用、
  压缩/重试/中断次数、任务完成/失败、当前阶段与轮次；`alerts()` 按阈值告警
  （token 速率过快、连续失败 ≥3 次、轮次逼近上限），供 TUI 监控视图渲染。

### 会话档案与回放（`agent/observability/archive.py`）
- `SessionArchive.write()` 把事件 + span + 决策日志 + 指标 + 结果打包为单个 JSON
  （`agent.session_archive_dir`，默认 `./logs/sessions`；`archive_enabled` 控制）。
- `SessionReplay` 按时间戳合并排序生成时间线，支持单步回放；CLI 用法：
  `python -m tui --replay logs/sessions/session_xxx.json`

### TUI 多视图与用户干预（`tui/app.py`）
- 纯终端 UI 设计（无 emoji / 无 256 色）：左栏任务面板（任务名 / 阶段 / 任务树 / 进度条 / 耗时，F6 可切换文件树）、
  主日志区（DataTable 三列虚拟滚动，`[HH:MM:SS] TYPE 内容` 八类语义色）、
  终端输出区（6 行，F3 全屏，D 键在原始输出与 diff 间切换）、底部状态栏（右对齐：tokens / round / mem / session）与输入栏；
- `F5` 轮换主区视图：主日志 / 文件变更（写操作渲染 unified diff）/ 监控（指标 + 告警）/ 时间线（span 耗时分布）；
- 窄屏（<100 列）自动降级为单栏（紧凑头 + 主日志），`F4` 手动宽/窄切换，`F2` 隐藏任务面板；
- 输入栏支持 `/pause` `/resume` `/status` `/retry` `/skip` `/quit` 命令与上下箭头历史；
- 高风险工具确认弹窗：命中 `agent.require_confirmation` 时弹出，支持
  `y`（批准一次）/ `a`（批准所有同类，写回 `loop._approve_rules`）/ `n`（拒绝）/
  `e:{"path":"..."}`（编辑参数后执行）；确认回调契约见 `tui/bridge.py` 的 `_on_confirmation`。

验证见 `tests/test_observability.py` 与 `tests/test_tui.py`。

## 接入真实模型与 MCP

- 修改 `config/agent.yaml`：`llm.provider: litellm`，填写 `model`（如 `openai/gpt-4o`）
  与 `api_key_env`（如 `OPENAI_API_KEY`）。
- MCP 服务器在 `config/mcp.yaml` 登记（stdio / sse）。`AgentLoop` 启动时经
  `agent/mcp/manager.py` 连接全部服务器、握手、合并工具，并按任务关键词订阅
  资源注入 Prompt（`agent/mcp/client.py`、`agent/mcp/tool.py`）。
- Docker 沙箱（`config/agent.yaml` 的 `sandbox.docker_enabled`）启用后，
  终端/文件工具将路由到容器内执行；当前由 `agent/sandbox/policy.py` 提供
  进程级防护作为默认兜底。

## Textual TUI（第 14.1 节）

```bash
python -m tui "分析当前项目结构并给出改进建议"
python -m tui --config config/agent.yaml "修复失败的测试"
```

- **左栏任务面板**：任务名 / 阶段（颜色区分）/ 任务树（完成·进行>·等待·失败）/ 进度条 / 耗时；`F6` 切换为文件树视图（ASCII 连线、/ 过滤搜索、Enter 预览、* 标记最近修改、> 标记当前操作）；
- **主日志区**：`[HH:MM:SS] TYPE 内容` 三列 DataTable 虚拟滚动（THINK 青 / ACT 亮白 / INFO 暗灰 /
  WARN 黄 / ERROR 红 / OK 绿），自动跟随 / 手动浏览模式，万级行流畅；
- **终端输出区**（6 行可滚动）：终端原始输出流（`TerminalTool` 逐行实时转发），`F3` 全屏；`D` 键切换为文件变更 diff（写前快照 unified diff）；
- **底部状态栏**（右对齐）：`tokens`（80% 变黄 / 95% 变红）、`round`（90% 变黄）、
  `mem`（记忆库用量 %）、`session`；
- **输入栏**：直接输入即注入高优先级指令；`/` 开头为命令（`/pause` `/resume` `/status`
  `/retry` `/skip` `/quit`），上下箭头浏览历史；
- **时间线视图**（`F5` 轮换到）：ASCII 火焰图展示 THINK/ACT/OBS span 的耗时分布，底部汇总总耗时 / 步数 / 最慢步骤（`tui/timeline_view.py` + `Tracer.get_timeline_data()`）；
- **快捷键**：`F1` 帮助 / `F2` 任务面板 / `F3` 终端全屏 / `F4` 宽窄切换 / `F5` 主区视图（日志/变更/监控/时间线）/ `F6` 文件树 /
  `D` 输出与 diff 切换 / `Ctrl+I` 注入 / `Ctrl+P` 暂停 / `Ctrl+R` 重试 / `Ctrl+S` 跳过 /
  `Ctrl+L` 清空终端 / `Tab` 切换窗格 / `q`、`Ctrl+C` 退出；
- 高风险操作（命中 `agent.require_confirmation`）弹出确认框：批准一次 / 批准所有同类 /
  拒绝 / 编辑参数后执行；会话结束后可用 `--replay` 按时间线回放档案。

实现要点：`AgentLoop.subscribe()` 实时事件订阅、`ExecutionContext.output_callback`
把命令输出逐行转发给右栏、Textual worker 在事件循环内跑 Agent 主循环
（`tui/bridge.py`、`tui/app.py`）。

## 路线图（按设计文档顺序深化）

1. ~~MCP 客户端（`mcp` Python SDK）初始化握手、工具合并与资源订阅~~（已完成，见 `agent/mcp/` 与第 13 节）；
2. ~~长期记忆升级为 Chroma/Qdrant 向量检索 + 自动经验摘要写入~~（已完成，见上节）；
3. ~~多 Agent 协作（Orchestrator/Worker + 黑板）与 Critic 仲裁~~（已完成，见 `agent/multiagent/` 与第 8 节）；
3.1 ~~代码语义理解层：AST 摘要 / 调用图 / 项目约定提取，Planner 注入影响范围~~（已完成，阶段一，见 `agent/code/`）；
3.2 ~~技能注册表规范化：requires/permissions/params、注册表合并、校验、发现、使用记录~~（已完成，阶段二，见 `agent/context/skill.py`）；
3.3 ~~阶段二后续项：真实技能库 + 意图过滤、技能管道与条件分支、自然语言创建技能、执行进度可视化~~（已完成，见 `agent/context/skill_author.py`、`scripts/save_skill.py`、`tests/test_real_project_suite.py`、`tests/test_skill_author.py`）；

4. ~~Docker 沙箱完整生命周期（网络隔离、资源限制、快照/回滚）~~（已完成，见 `agent/sandbox/docker_sandbox.py`）；工具层安全加固（网络细粒度策略/假网络/受保护路径/审计回滚/资源熔断）已完成（阶段五）；
5. ~~MCP 生态打通（断连重连/降级、资源缓存 TTL、自研 TS 服务器）~~（已完成，阶段六，见 `agent/mcp/manager.py` 与 `mcp-servers/`）；
6. ~~Textual TUI 三栏布局~~（已完成，见 `tui/` 与第 14 节）+ WebSocket 事件订阅（待接入）；
7. ~~可观测性：分布式追踪 / 实时指标 / 会话档案与回放~~（已完成，阶段七，见 `agent/observability/`）；
8. ~~TUI 交互升级：多视图 / 确认弹窗 / 修改参数后执行 / 回放 CLI~~（已完成，阶段八，见 `tui/`）；
9. Web 观测面板（React + WebSocket 甘特图/火焰图，可选，见设计第 14.4 节）；
10. OpenTelemetry 导出到 Jaeger 与结构化 JSON 日志。
