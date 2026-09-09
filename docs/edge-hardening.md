# 边界打磨：审计结论与加固记录

面向“输入验证 / 状态机 / 工具与沙箱安全 / 并发与资源 / 密钥脱敏”五类边界的一次
系统性审计与加固。原则：先核对已有防护，确有缺口才改最小面；每项修复配回归测试。

## 1. 审计结论（已具备的防护，未重复实现）

- **状态机**：`agent/core/state.py` 状态转移校验已完备——非法跳跃
  （如 `IDLE→RUNNING`）、`COMPLETED` 后再推进、原地重复推进均抛 `ValueError` 且状态不变。
- **工具层**：`agent/tools/terminal.py` 已无 `shell=True`、全部显式 argv、超时
  terminate→kill 升级、输出三级截断；`fileio.py` 已做 resolve 越界拦截、写前快照、审计回滚。
- **沙箱**：`agent/sandbox/policy.py` 已有命令黑名单、受保护路径防删、网络策略；
  `test_tool.py` 对可执行缺失/未知框架/lint 解析失败均结构化降级。
- **密钥现状**：对 git 跟踪文件的全量扫描（`sk-`/`AKIA`/`ghp_`/`github_pat_`/
  private key/Bearer/URL 内嵌凭据等模式）仅命中 `docker-compose.yml` 的
  `DEEPSEEK_API_KEY` 环境变量透传，无硬编码真实密钥。

## 2. 本轮修复（最小面，全部带测试）

### 输入与恢复边界（`tests/test_edge_input_state.py`，13 例）
- CLI 任务描述长度上限 `MAX_PROMPT_CHARS = 200_000`：位置参数与 stdin 两条路径超限
  均抛 `UsageError`（退出码 2），信息含当前长度与上限。
- 快照恢复：垃圾 JSON、缺 `tasks` 字段、非法任务字段等损坏快照不再崩溃或误报
  `resume.restored`，统一降级为 `resume.no_snapshot`/`resume.fallback` 决策并重新规划。
- 配置加载：候选层字段类型错误/越界取值（如 `llm.temperature=99`）登记
  `CONFIG_FALLBACKS` + WARN 后逐层降级到默认，`load_config` 任何情况不抛异常。

### 工具与沙箱命令安全（`tests/test_edge_tools_security.py`，19 例）
- 字面参数（`; && | $()`、%0a、`-n` 等）经真实子进程证明不被 shell 二次解释；
  超时、超大输出截断、工作区外路径穿越（`..`/URL 编码/盘符）均回归锁定。
- `policy.py`：删除命令解析按“空白 + shell 分隔符（`; & | ( ) < > 反引号`）”切分，
  新增根目录/盘根删除守卫，封堵 `rm -rf  /`、`rm -fr /`、`rm --recursive --force /`、
  `Remove-Item … | …`、`; rm -rf .git` 等绕过变体；合法删除不受影响。

### 并发锁与密钥脱敏（`tests/test_edge_concurrency_redact.py`，16 例）
- `project_lock.py`：损坏 pid/时间戳不再崩溃；持有者进程已死的残留锁立即接管
  （原实现要求 age>5s，刚崩溃锁无法在 timeout=0 下回收）；回收前 TOCTOU 二次校验。
- 同 db 两个 asyncio 任务并发 remember+search 不崩不丢。
- 新增 `agent/redact.py`：`redact_secrets()`/`redact_dict()`/`redact_value()`
  保守脱敏（`sk-` 长串、`Bearer`、authorization 头、URL 内嵌凭据、敏感键名
  整段值），词元边界匹配避免误伤 `monkey/keyboard/keyword` 与普通中英文。
  并已接入主流程：
  - `python -m agent run` 组装完 payload 后先整体脱敏再 `_emit`——`--output
    json|text` 的 stdout、失败时的 stderr 都不会回显最终答复/错误/归因文本中
    夹带的真实密钥；
  - 统一错误出口 `write_error_log`/`print_error` 落盘与打印内容（异常消息、
    上下文值、traceback）过同一套脱敏规则，`logs/cli_error_*.log` 不泄漏密钥。

## 3. 验证与已知限制
- 新测试本地合计：43 passed / 5 skipped（skip 为沙箱禁止 asyncio+PIPE 子进程的
  环境限制，对应真实子进程路径已在升级权限探针中单独验证；CI ubuntu 无此限制）。
- 脱敏接入回归 `tests/test_redact_wiring.py`（3 例）与既有
  `test_errorlog` / `test_cli_dryrun_resume` 等相邻回归本地通过（仅 1 个 config
  用例因沙箱 ACL 禁写 `%TEMP%` 的 pytest `tmp_path` 无法本地跑，CI ubuntu 无此限制）。
- 回归：`test_state` / `test_loop_resume` / `test_cli_dryrun_resume` /
  `test_long_task_cli` / `test_output_truncation` / `test_concurrency_multi` /
  `test_config` / `test_sandbox_security` 合计 56 passed；仅 2 个既有
  circuit-breaker 用例在本沙箱因“启动进程 WinError 5”失败（环境 ACL，非改动回归）。
- 仍建议在真实 CI（ubuntu）跑一遍 `test_edge_tools_security.py` 以覆盖 5 个
  真实子进程用例。

## 4. 跨平台 quality-gate 矩阵复测

- `.github/workflows/quality-gate.yml` 的 pytest 离线套件扩为 ubuntu / windows /
  macos 三平台矩阵（`fail-fast: false`；windows/macos 的安装与测试 step 显式
  `shell: bash`），lint 与 docker 构建保持 ubuntu 单跑。
- 首轮实测：ubuntu 3m6s ✅、macos 3m21s ✅、windows 4m21s ❌ 2 例——均非平台
  业务差异，而是粗时钟 / 信号顺序两类边界：
  - 快照文件名：Windows 时钟粒度毫秒级，紧邻两次 `_save_snapshot()` 取到相同
    微秒值导致同名覆盖只剩 1 个文件。`_snapshot_timestamp()` 改为记录上次
    返回值、时钟未前进则补 1 微秒（严格单调递增），并新增同刻度回归用例
    `test_snapshot_timestamp_monotonic_on_coarse_clock`。
  - 失败归因：`degenerate_abort`（连续空参数保护中止）此前排在“过早压缩 →
    context”之后，Windows 运行伴生一次早期压缩即被误判 context。归因规则把
    `degenerate_abort` 提到最前固定归 tool，新增
    `test_classify_degenerate_abort_beats_premature_compression` 锁定。
- 修复后本地（Windows）`test_loop_resume` + `test_p2_stability_attribution`
  合计 32 passed；toxiproxy 网络故障注入留作后续。
