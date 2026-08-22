# 架构总览

> 本文档从根 README 迁移，保留「目录结构」「两套架构」与「设计与实现的对应关系」。
>
> **新旧架构边界**：顶层 `loop.py`、`scheduler.py`、`main.py` 等为旧版七层原型（仅作对照参考，不再演进）；真正主干在 `agent/` 包内。

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
tests/                  新核心测试（760 项，含故障注入/浸泡/混沌/基准集）
loop.py, scheduler.py, ... 旧版七层原型（保留，作为对照参考）
```


## 两套架构（重要）

仓库同时存在两套独立实现，入口与默认配置不同，请勿混用：

| 架构 | 入口 | 配置 | 状态 |
| --- | --- | --- | --- |
| **新核心（推荐）** | `python -m agent run "任务"`、`python -m tui` | `config/agent.yaml`（Pydantic 校验） | 主干，功能齐全 |
| 旧版七层原型 | `main.py`、`test_all.py` | `config.yaml`（根目录，无校验） | 仅作对照参考，不再演进 |

两套默认值不一致（记忆后端、LLM provider 等）：新核心默认 `memory.backend: hybrid`
（本地 SQLite + TF-IDF，离线持久），旧原型默认可能指向 Chroma/在线模型。任务级配置以
各自入口加载的配置文件为准。


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
| 14 可观测性 | `tui/`（Textual 三栏）+ `AgentLoop.subscribe()` + 终端实时输出回调 + `agent/observability/web.py` + `agent/observability/otel.py` | 已实现（思维流/终端流/状态栏/Ctrl+I 中断/Ctrl+P 暂停）；Web 观测面板（`--web`）与 OTLP/Jaeger 导出、结构化 JSONL 日志（第 9/10 节） |
| 配置→运行时数据流 | `agent/config.py` + `agent/core/decision_logger.py` + `agent/sandbox/docker_sandbox.py` + `scripts/analyze_decisions.py` | 已实现：YAML→Pydantic→组件工厂→运行时决策点；JSONL 决策日志；default/aggressive A/B 对比测试 |


相关文档：[getting-started.md](getting-started.md)（首次运行）、[configuration.md](configuration.md)（配置系统）、[index.md](index.md)（文档导航）。

