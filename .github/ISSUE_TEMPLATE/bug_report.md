---
name: Bug report
about: 报告一个可复现的缺陷
title: "[bug] "
labels: ["bug"]
assignees: ""
---

**描述**
清晰描述问题现象与影响（哪个入口：`python -m agent run` / `python -m tui` / Web / CLI）。

**复现步骤**
1. 使用的命令或配置（可脱敏）
2. 期望行为
3. 实际行为

**日志与上下文**
- 粘贴关键报错/决策记录（`logs/`、决策 JSONL、trace）
- 相关配置文件版本（`config/agent.yaml` 关键片段）

**环境**
- OS / Python 版本 / 是否 Docker
- LLM provider 或 mock
- 最近一次通过的提交（如已知）

**检查清单**
- [ ] 已确认不是配置或环境问题
- [ ] 已确认仓库处于最新 master
