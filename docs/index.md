# Alpha-SWE Agent 文档

> 文档导航中心。完整深度内容从根 README 迁移至此；功能模块见 `docs/features/`，历史验证报告见 `docs/` 下的 `01-09` 编号文档。

## 快速上手

- [getting-started.md](getting-started.md) — 安装、离线/在线首次运行、接入真实模型与 MCP
- [configuration.md](configuration.md) — 配置系统、配置到运行时数据流与决策日志分析
- [architecture.md](architecture.md) — 目录结构、两套架构与设计与实现对应关系

## 功能模块

- [features/memory.md](features/memory.md) — 长期记忆（写入/检索/去重/衰减/多后端）
- [features/code-semantics.md](features/code-semantics.md) — 代码语义理解（AST/调用图/项目画像）
- [features/multilang.md](features/multilang.md) — 多语言支持与扩展工具（含 VS Code 扩展）
- [features/skills-plugins.md](features/skills-plugins.md) — 技能与插件（动态注入/工作流/自然语言创建）
- [features/multiagent.md](features/multiagent.md) — 多 Agent 协作
- [features/sandbox.md](features/sandbox.md) — 沙箱安全与 Docker 容器
- [features/mcp.md](features/mcp.md) — MCP 生态
- [features/observability.md](features/observability.md) — 可观测性（追踪/指标/Web 面板/OTLP）
- [features/tui.md](features/tui.md) — Textual TUI 多视图与用户干预

## 项目现状与验证

- [status.md](status.md) — 项目现状核对（2026-08 审计）与真实项目验证
- 历史验证报告：`01-docker-verification.md`、`02-concurrency-verification.md`、`03-real-project-verification.md`、`04-multi-instance-guide.md`、`05-swebench-benchmark.md`、`06-productization-service.md`、`07-code-quality.md`、`08-multilang-tools.md`、`09-open-source-prep.md`

## 其他

- 路线图：[../ROADMAP.md](../ROADMAP.md)（仓库根目录）
- 变更日志：[../CHANGELOG.md](../CHANGELOG.md)（仓库根目录）
- 贡献指南：[../CONTRIBUTING.md](../CONTRIBUTING.md)（仓库根目录）

