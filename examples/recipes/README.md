# 场景配置模板（recipes）

每种模板是一份可直接落地的 `config/agent.yaml` 片段或整文件，按任务类型取舍能力：

- `fast-fix.yaml`：快速修复小 bug——轻量、低预算、先写测试再修。
- `deep-refactor.yaml`：深度重构——开启门禁与回归保护，慢而稳。
- `multi-agent.yaml`：多 Agent 协作——Planner/Executor/Critic 分工。

用法：把模板对应片段合并进 `config/agent.yaml`，或用
`python -m agent run "任务" --config examples/recipes/fast-fix.yaml --workspace <项目路径>`。
