# Alpha-SWE Agent

基于设计文档落地的最小但可扩展的 SWE Agent 系统。核心通信完全异步（asyncio），
任务以 DAG 调度，支持长期记忆、技能注入、上下文压缩、安全沙箱与用户中断。

## 目录结构

```
agent/                  新核心架构（本设计的主干）
├── core/               异步主循环、Task/TaskDAG、状态机、调度器
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
├── config.py           Pydantic + YAML 配置
└── llm.py              模型接口（mock / litellm）
config/
├── agent.yaml          Agent 全局配置
└── mcp.yaml            MCP 服务器清单
examples/quick_demo.py  脚本化 LLM 的端到端演示
tests/                  新核心测试（66 项）
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
| 7 长期记忆 | `agent/memory/` | 已实现（混合向量检索，Chroma/Qdrant/Hybrid 可插拔；经验摘要/代码索引/错误记忆自动写入） |
| 10 技能/插件 | `agent/context/manager.py` | 基础版（关键词激活；可扩展文件类型匹配） |
| 11 上下文压缩 | `agent/context/manager.py` | 基础版（长输出截断 + 阶段摘要） |
| 12 沙箱 | `agent/sandbox/policy.py` | 路径锚定/危险命令拦截；Docker 容器隔离预留 |
| 13 MCP | `agent/mcp/`（client/manager/tool）+ `config/mcp.yaml` | 已实现（stdio/sse 客户端、握手、工具合并、资源订阅注入 Prompt） |
| 14 可观测性 | `AgentLoop.events`、`structured_log.py` | 事件流已内置；TUI/OTel 面板待接入 |

## 长期记忆（第 7 节）

- **写入**：任务完成后 LLM 自动生成经验摘要（problem/steps/solution/outcome/key_files），
  失败时记录错误记忆（错误类型 + 上下文），文件写入/读取后自动索引代码（路径 + 符号 + 片段）。
- **检索**：任务指令触发混合检索（向量相似度 + 关键词打分，`hybrid_weight_vector` 调权重），
  结果注入 Prompt 的「检索到的历史记忆」区块。
- **后端**：`config/agent.yaml` 的 `memory.backend`：
  - `auto`：有 `chromadb` 用之，否则有 `qdrant-client` 用之，否则本地 `hybrid`（TF-IDF，零新依赖）；
  - `sqlite`：纯关键词（最轻）；`hybrid`：本地向量 + 关键词；`chroma` / `qdrant`：真实向量库。
- **嵌入器**：`memory.embedder` 支持 `tfidf`（默认）、`sentence-transformers`（本地模型）、
  `openai`（Embeddings API，`embedding_api_key_env` 指定密钥环境变量）。

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

## 接入真实模型与 MCP

- 修改 `config/agent.yaml`：`llm.provider: litellm`，填写 `model`（如 `openai/gpt-4o`）
  与 `api_key_env`（如 `OPENAI_API_KEY`）。
- MCP 服务器在 `config/mcp.yaml` 登记（stdio / sse）。`AgentLoop` 启动时经
  `agent/mcp/manager.py` 连接全部服务器、握手、合并工具，并按任务关键词订阅
  资源注入 Prompt（`agent/mcp/client.py`、`agent/mcp/tool.py`）。
- Docker 沙箱（`config/agent.yaml` 的 `sandbox.docker_enabled`）启用后，
  终端/文件工具将路由到容器内执行；当前由 `agent/sandbox/policy.py` 提供
  进程级防护作为默认兜底。

## 路线图（按设计文档顺序深化）

1. ~~MCP 客户端（`mcp` Python SDK）初始化握手、工具合并与资源订阅~~（已完成，见 `agent/mcp/` 与第 13 节）；
2. ~~长期记忆升级为 Chroma/Qdrant 向量检索 + 自动经验摘要写入~~（已完成，见上节）；
3. 多 Agent 协作（Orchestrator/Worker + 黑板）与 Critic 仲裁；
4. Docker 沙箱完整生命周期（网络隔离、资源限制、快照/回滚）；
5. Textual TUI 三栏布局 + WebSocket 事件订阅；
6. OpenTelemetry 追踪导出与结构化 JSON 日志。