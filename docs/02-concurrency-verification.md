# 方向一 · 阶段 2：多用户/多实例并发验证报告

- 验证日期：2026-08-18
- 验证套件：`tests/test_concurrency_multi.py`（9 项）
- 相关回归：`tests/test_cross_integration.py`（单进程多 Agent 共享记忆）、`tests/test_soak.py`（长时间浸泡）
- 运行方式：`python -X utf8 -m pytest tests/test_concurrency_multi.py -q`（跨进程用例走独立 Python 子进程，贴近真实多实例）

## 一、验证结果总览

| 验证点 | 结果 | 说明 |
|---|---|---|
| A. 同一项目并发访问（文件写锁） | ✅ | 同文件第二 Agent 写入被拒绝并返回冲突提示（holder 信息）；不同文件并行写互不干扰 |
| B. 同一记忆后端并发读写 | ✅ | 4 进程并发写同一 SQLite 无 "database is locked"、无丢失更新（计数精确）；多线程去重/仲裁并发下仍有效 |
| C. 同一项目锁文件互斥 | ✅ | 跨进程仅一方获锁；第二实例被明确拒绝；释放后恢复；持有者崩溃的残留锁自动回收 |
| D. 会话状态共享与隔离 | ✅ | 会话档案隔离；项目级记忆跨实例共享 |
| 压力测试 | ✅ | 8 进程 x 20 条并发写入：计数精确、无异常 |
| 故障注入 | ✅ | 残留锁（死 PID）回收；`AgentLoop` 集成下第二实例启动抛 `ProjectLockError` |

9 项全部通过。

## 二、发现并修复的问题

### 1. SQLite 后端无并发写保护（已修复）

- **现象**：`SqliteMemoryStore` / `HybridMemoryStore` 以默认参数 `sqlite3.connect(...)` 连接，无 `busy_timeout`，journal 模式为 DELETE。多进程并发写同一 `memory.db` 时出现 `database is locked`，且写锁竞争下易丢更新。
- **修复**（`agent/memory/store.py`）：
  - `sqlite3.connect(db_path, check_same_thread=False, timeout=30)` —— busy_timeout 30s；
  - 连接后执行 `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL`，允许并发读 + 单写者，显著降低锁冲突。
- **验证**：4 进程并发写 40 条、8 进程并发写 160 条，计数精确、无异常。

### 2. 跨进程项目互斥缺失（已修复）

- **现象**：`Blackboard.lock_file` 是进程内内存锁，只覆盖单进程多 Agent；两个独立 Agent 进程指向同一项目目录时没有任何互斥。
- **修复**：
  - 新增 `agent/project_lock.py`：`ProjectLock` 用原子 `O_CREAT|O_EXCL` 创建 `.swe-agent/project.lock`，锁内记录 `pid/holder/acquired_at`；冲突时读取锁文件，持有者 PID 已不存在则自动回收残留锁（psutil.pid_exists 跨平台）。
  - `AgentLoop` 集成：`agent.project_lock_enabled: true` 时 `run()` 获取项目锁，被其他实例持有则抛 `ProjectLockError`（明确提示持有者与 pid）；`close()` 释放。`project_lock_timeout` 支持等待。
- **验证**：跨进程互斥、残留锁回收、Loop 集成拒绝第二实例、释放后恢复，全部通过。

### 3. 既有 flaky：`test_project_state.py::test_file_change_detected_across_sessions`

- 与本次改动无关的既有问题：项目状态 tracker 以 mtime 检测文件变化，测试在同秒内写文件时可能漏检（约 2/3 概率失败）。建议后续把文件快照比较改为内容哈希 + mtime 双重判断。

## 三、多实例部署建议（详见 `docs/03-multi-instance-guide.md`）

1. 多实例指向同一项目时开启 `agent.project_lock_enabled: true`，避免双写冲突。
2. 共享记忆后端使用 SQLite（WAL 已默认启用）；向量后端（Chroma/Qdrant）本身支持多写者。
3. `SharedMemoryStore` 的写入冲突仲裁在跨实例场景同样生效（SQLite 事务兜底）。
4. 会话档案目录按实例隔离（默认已按配置目录区分），不要共用。
