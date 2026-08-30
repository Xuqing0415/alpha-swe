# Alpha-SWE Agent

[![CI](https://github.com/Xuqing0415/alpha-swe/actions/workflows/quality-gate.yml/badge.svg)](https://github.com/Xuqing0415/alpha-swe/actions/workflows/quality-gate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)]()

基于设计文档落地的最小但可扩展的 SWE Agent 系统：核心通信完全异步（asyncio），
任务以 DAG 调度，支持长期记忆、技能注入、上下文压缩、安全沙箱、多 Agent 协作与用户中断。

## 核心特性

- **异步状态机 + DAG 调度**：任务级重试、三级降级、断点续跑、并行工具调用、用户确认/自动批准
- **长期记忆闭环**：经验/代码/错误记忆，多后端可插拔（Hybrid/SQLite/Chroma/Qdrant），去重、反例降权、可信度衰减
- **技能与插件**：YAML 工作流、自然语言创建技能、按文件/关键词/依赖动态注入上下文
- **安全沙箱**：路径/命令/网络策略、审计回滚、资源熔断、Docker 容器生命周期
- **多 Agent 协作**：可插拔角色（Orchestrator/Worker + 黑板）、冲突仲裁与人工介入
- **完整可观测性**：Trace/指标/决策日志/会话回放、Web 面板与 Textual TUI

## 两套架构（重要）

仓库同时存在两套独立实现，入口与默认配置不同，请勿混用：

| 架构 | 入口 | 配置 | 状态 |
| --- | --- | --- | --- |
| **新核心（推荐）** | `python -m agent run "任务"`、`python -m tui` | `config/agent.yaml`（Pydantic 校验） | 主干，功能齐全 |
| 旧版七层原型 | `main.py`、`test_all.py` | `config.yaml`（根目录，无校验） | 仅作对照参考，不再演进 |

完整说明（项目结构与设计与实现对应关系）见 [docs/architecture.md](docs/architecture.md)。

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

# TUI 模式 / Web 观测面板
python -m tui "分析当前项目结构并给出改进建议"
python -m tui --web "任务提示词"   # 打开 http://127.0.0.1:8765
```

**默认即离线可跑**：`config/agent.yaml` 出厂为本地 hybrid 记忆 + TF-IDF，零外部依赖；
完全离线演示用 `python -m agent run "任务" --config config/offline.yaml`（内置 MockLLM）。
更多安装、首次运行与接入真实模型/MCP 见 [docs/getting-started.md](docs/getting-started.md)。

## 文档

完整深度文档已迁移到 `docs/`，入口 [docs/index.md](docs/index.md)：

- [docs/architecture.md](docs/architecture.md) — 架构总览与设计实现对应关系
- [docs/configuration.md](docs/configuration.md) — 配置系统与决策日志分析
- [docs/status.md](docs/status.md) — 项目现状核对（2026-08 审计）与真实项目验证
- `docs/features/` — 记忆、代码语义、多语言、技能/插件、多 Agent、沙箱、MCP、可观测性、TUI
- `docs/01`-`09` — Docker/并发/真实项目/SWE-bench/产品化/代码质量/多语言/开源准备等历史验证报告

## 阶段门禁（phase-barrier 集成）

接入 [phase-barrier](https://github.com/Xuqing0415/phase-barrier)（PyPI：[phase-barrier](https://pypi.org/project/phase-barrier/)）作为阶段门禁中间件（[alpha-swe#1](https://github.com/Xuqing0415/alpha-swe/issues/1)）：

- **任务启动钩子**：`run()` 启动时检查门禁阶段（默认阶段 1=Spec 设计），未满足时把约束提示注入 System Prompt；
- **阶段切换钩子**：`file_ops` 写实现 / `terminal_execute` 测试命令 / `run_tests` 在未达到前置阶段时被拦截，约束消息回传 Agent 强制补全 spec / 测试用例；
- **门禁工具**：`phase_barrier_gate`（inspect / check / advance / record_test_run / verify）供 Agent 声明与推进阶段；
- **轻量 SDK**：alpha-swe 只做调用，校验逻辑全部在 phase-barrier 仓库维护；依赖缺失 / 初始化失败自动降级放行，不影响既有行为。

启用方式（`config/agent.yaml`，默认关闭）：

```yaml
phase_barrier:
  enabled: true          # 开启阶段门禁
  workdir: ""            # 空 = 使用 sandbox.workspace
  user_request: ""       # 阶段 0 证据：用户需求原文（留空则用任务 prompt）
  task_start_stage: 1
  implementation_stage: 3
  test_run_stage: 4
  timeout: 10
```

端到端测试见 `tests/test_phase_barrier.py`（跳步写实现被拦截 + 按 SOP 推进到交付）。
## 项目状态

全量测试通过；真实 LLM（litellm）与真实 Docker 沙箱均已端到端验证，细节见
[docs/status.md](docs/status.md)。路线图 [ROADMAP.md](ROADMAP.md) · 变更日志
[CHANGELOG.md](CHANGELOG.md)。

## 贡献

贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)（Conventional Commits）；本项目使用
[MIT 许可证](LICENSE)。
