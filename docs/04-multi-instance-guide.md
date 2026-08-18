# 方向一 · 阶段 2 产出：多实例部署指南

- 对应验证报告：`docs/02-concurrency-verification.md`
- 适用范围：两个及以上 Alpha-SWE Agent 实例同时运行（同一项目或共享记忆后端）

## 一、部署拓扑

| 场景 | 说明 | 推荐配置 |
|---|---|---|
| 不同项目、独立记忆 | 各实例互不干扰，最简单 | 默认配置即可 |
| 不同项目、共享记忆 | 会话隔离 + 项目级记忆共享 | 同一 `memory.db` / Qdrant；开启 WAL |
| 同一项目（互斥） | 同一时刻只允许一个实例写该项目 | `project_lock_enabled: true` |
| 同一项目（并行只读/研究） | 只读分析可并行，但写入互斥 | 项目锁 + 文件级写锁同时开启 |

## 二、同一项目互斥（推荐开启）

在 `config/*.yaml` 中配置：

```yaml
agent:
  project_lock_enabled: true   # 多实例写同一项目时开启
  project_lock_timeout: 10     # 等待锁的秒数（0=立即失败）
  project_lock_holder: "dev-1" # 可读的持有者标识（默认 pid-<pid>）
```

行为：

- 第二个实例启动时若锁被存活进程持有，`run()` 抛 `ProjectLockError`，CLI 给出
  「项目已被 <holder>（pid=<pid>）锁定」的明确提示并快速退出（不阻塞）。
- 持有者进程崩溃（kill -9 / 断电）后，锁文件内的 PID 不存在时新实例自动回收残留锁。
- `close()` 正常释放锁；`stale_after_seconds`（默认 300s）作为超时兜底。

注意：项目锁保证「同一项目目录互斥」，但**不**替代文件级写锁。多 Agent 并行写
同一仓库时请同时开启黑板文件锁（默认开启，`blackboard.file_locks_enabled: true`），
避免同文件并发覆盖。

## 三、共享记忆后端（SQLite / Qdrant）

### SQLite（单机小规模）

- 已内置并发保护：`timeout=30`（busy_timeout）+ `PRAGMA journal_mode=WAL` +
  `synchronous=NORMAL`（`agent/memory/store.py`）。
- 实测 4 进程并发写 40 条 / 8 进程并发写 160 条：无 `database is locked`、无丢失更新。
- 建议：共享 `memory.db` 放在网络盘上时走 NFS/SMB 的 `mmap` 行为可能不一致，
  生产环境仍建议放本机磁盘或容器卷。

### Qdrant（中高并发）

- 切换到 Qdrant 后端（`memory.backend: qdrant`）可获得真正的多写者并发；
  SQLite WAL 是「单写者 + 多读者」，写入密集场景请优先 Qdrant。

### 并发写入去重

- 记忆去重在写入路径内置「事务内先查后写 + 唯一约束」，并发下不会重复写入
  相同/高相似记忆（验证见 `tests/test_concurrency_multi.py`）。

## 四、真实 Docker 沙箱在多实例下的注意事项

- 容器是**单实例内**隔离边界（任务级），多实例各自拥有容器池；共享宿主机时
  请为每个实例设置不同的 `sandbox.workspace`，避免 bind 挂载冲突。
- 磁盘：每个容器 + 快照镜像都会占空间，多个实例同时跑时按
  `实例数 × (镜像 ~1GB + 快照 × max_snapshots)` 预留（参考 `docs/01-docker-verification.md`）。
- 资源限制：`memory_limit` / `cpu_limit` 是容器级硬限制，防止单个实例拖垮宿主；
  实例数量 × 单容器内存上限 ≤ 宿主机可用内存。

## 五、故障恢复建议

- 任务超时/预算耗尽后，受影响的实例自行失败并释放锁；其它实例不受影响（会话隔离）。
- 残留锁自动回收依赖 PID 存活检测；跨机器共享项目目录时 PID 可能冲突，
  请改用 `project_lock_holder` 显式命名 + 人工清理 `.swe-agent/project.lock`。
- 共享记忆库建议定期备份；SQLite 用 `VACUUM INTO` 做在线备份，Qdrant 用快照 API。
