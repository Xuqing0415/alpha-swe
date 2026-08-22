# 沙箱安全与 Docker 容器

> 本文档从根 README 迁移（原「沙箱安全加固（阶段五）」与「Docker 沙箱容器生命周期（第 12 节）」）。

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


真实 Docker 验证报告：[../01-docker-verification.md](../01-docker-verification.md)。

