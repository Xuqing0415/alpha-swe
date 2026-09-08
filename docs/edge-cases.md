# 边界场景与故障排查

> 本文档汇总系统在各类边界条件下的既有处理机制、对应配置与验证位置，便于了解预期行为与补充测试。
> 机制以代码与配置为准；配置示例见 `config/agent.yaml`。

## 超长任务与长输出

- 上下文总量受 `context.max_tokens` 控制，达到 `compression_threshold` 触发分级压缩：`compression_method` 支持 light（截断工具输出）/ medium（保留决策点）/ heavy（递归摘要）；被压缩的原始输出先存档到文件，Prompt 中保留引用路径。
- 终端工具输出三级截断（全文 / 头尾+存档 / LLM 摘要），并提取关键行；长输出不会整体填入上下文。
- 任务轮次受 `agent.max_rounds` 限制；token 预算耗尽时任务以明确原因失败退出（“任务预算耗尽”），而非静默挂起。

验证：`tests/test_compression_quality.py`、`tests/test_loop_*.py`。

## 空任务 / 畸形输入

- 规划器解析失败或空指令回退为单任务；空参数与 think+tool 组合调用均有健壮性处理。
- 输出解析失败带重试反馈，`reasoning` 空内容会被拒绝。

## 网络中断 / 外部服务不可用

- MCP：`mcp.reconnect_attempts` 控制断连重连；重连失败服务器进入 `failed_servers`，其工具从注册表降级隐藏，任务继续。
- 可观测导出：OTLP 导出失败静默降级，本地 JSONL 始终保留。
- 沙箱网络：`network_policy`（deny / allowlist / allow）+ `fake_network` 假响应，用于离线与受控测试。

验证：`tests/test_mcp_phase6.py`、`tests/test_observability.py`、`tests/test_sandbox_security.py`。

## 资源耗尽

- 命令超时：超时后 SIGTERM → SIGKILL 升级回收子进程；连续超时触发熔断。
- 内存熔断：`resource_monitor: true` 时按 `memory_limit_mb` 周期采样，超限 kill 整棵进程树并返回 `circuit_breaker=True`。
- Docker 沙箱：`memory_limit` / `cpu_limit` 映射容器 cgroup 限额，命令级超时强杀。

验证：`tests/test_tools_*.py`、`tests/test_sandbox_security.py`、`tests/test_docker_sandbox.py`。

## LLM 不可用 / 缺少密钥

- 完全离线：`python -m agent run "任务" --config config/offline.yaml`使用内置 MockLLM + hybrid 记忆，零网络零 Key。
- 线上缺 Key：CLI 快速失败并给出清晰报错，而不是卡死或空转。
- 记忆嵌入：无网络时从 sentence-transformers 回退 TF-IDF。

## 沙箱违规与写坏文件

- 危险命令/写系统路径被 `block_commands`、`protected_paths`（含 `.git`、`config/*.yaml`）拦截并计入 `violation_count`。
- 文件写入前有快照备份，审计记录 before/after，可用 rollback 恢复。
- `syntax_check_enabled`（默认开）：写入 Python 后即时语法检查，坏文件立即反馈而非等到运行期。
- `file_ops` 精确行编辑以读取时内容为准，文件被外部改动时返回冲突而非误改。

验证：`tests/test_sandbox_security.py`、`tests/test_tools_*.py`。

## 并发与多实例冲突

- 同项目多实例由项目锁与文件级写锁仲裁；共享记忆走 SQLite WAL（跨进程安全）。
- 详见 `docs/02-concurrency-verification.md`、`docs/04-multi-instance-guide.md`。

## 配置坏值

- 配置三层降级加载：用户指定路径 → 项目根目录 → 内置默认；单层失败不崩溃，逐层 try-except。
- 决策日志（`logs/decision_log*.jsonl`）记录每个配置键的运行时决策，坏键可定位。

验证：`tests/test_config.py`、`tests/test_config_impact.py`。

## 整体降级验证

故障注入套件把 LLM / 记忆 / 沙箱 / MCP 依次“弄坏”，断言系统优雅降级而非崩溃：`tests/test_fault_injection.py`；
长时间运行有界性见 `tests/test_soak.py`（多会话顺序/并发流，内存/句柄/事件列表有界）。
