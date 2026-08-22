# 多 Agent 协作

> 本文档从根 README 迁移（原「多 Agent 协作（第 8 节）」），入口 `agent/multiagent/`，团队配置见 `config/team.yaml`。

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

