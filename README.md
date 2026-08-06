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
| 12 沙箱 | `agent/sandbox/policy.py` | 路径锚定/危险命令拦截；Docker 容器隔离预留 |
| 13 MCP | `agent/mcp/`（client/manager/tool）+ `config/mcp.yaml` | 已实现（stdio/sse 客户端、握手、工具合并、资源订阅注入 Prompt） |
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

- 技能命中时由 `SkillLibrary.expand()` 展开为 Task DAG（步骤依赖 -> 任务依赖），替代 LLM 规划器；
- 步骤决策点写入 `Task.metadata`：`on_failure=fallback` 时失败自动 `spawn` 回退任务（`skill.step_fallback`），
  `orchestrate` 时发出 `skill_intervention` 事件请求介入；
- 决策日志：`skill.activate` / `skill.expand` / `skill.step_fallback` / `skill.step_intervention`。

## 快速开始

```powershell
# 安装依赖（已安装则跳过）
pip install pyyaml pydantic jinja2 pytest pytest-asyncio litellm
# 可选：真实向量库后端
pip install qdrant-client    # 或 chromadb

# 运行测试（终端工具测试会真实创建子进程）
python -X utf8 -m pytest tests -q

# 最小演示：脚本化 LLM 驱动一次完整 ReAct（terminal -> final_answer）
python -X utf8 examples/quick_demo.py
```

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
`planner.*`（拆分阈值 / 子任务上限 / 串并行）、`agent.*`（循环上限 / 工具并行 / 确认与自动批准）。

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

- **左栏**：思维流（思考 / 工具调用 / 任务事件，颜色区分，自动滚动）；
- **右栏**：终端原始输出流（`TerminalTool` 逐行实时转发）；
- **底部状态栏**：当前任务、按状态统计、轮次、token 估算、耗时、运行/暂停；
- **Ctrl+I** 注入高优先级指令（打断当前循环），**Ctrl+P** 暂停/继续，
  **Ctrl+L** 清空终端，**Tab** 切换窗格，**q / Ctrl+C** 退出。

实现要点：`AgentLoop.subscribe()` 实时事件订阅、`ExecutionContext.output_callback`
把命令输出逐行转发给右栏、Textual worker 在事件循环内跑 Agent 主循环
（`tui/bridge.py`、`tui/app.py`）。

## 路线图（按设计文档顺序深化）

1. ~~MCP 客户端（`mcp` Python SDK）初始化握手、工具合并与资源订阅~~（已完成，见 `agent/mcp/` 与第 13 节）；
2. ~~长期记忆升级为 Chroma/Qdrant 向量检索 + 自动经验摘要写入~~（已完成，见上节）；
3. ~~多 Agent 协作（Orchestrator/Worker + 黑板）与 Critic 仲裁~~（已完成，见 `agent/multiagent/` 与第 8 节）；

4. Docker 沙箱完整生命周期（网络隔离、资源限制、快照/回滚）；
5. ~~Textual TUI 三栏布局~~（已完成，见 `tui/` 与第 14 节）+ WebSocket 事件订阅（待接入）；
6. OpenTelemetry 追踪导出与结构化 JSON 日志。
