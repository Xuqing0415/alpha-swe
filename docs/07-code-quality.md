# 07 · 技术债务清理与稳定性加固报告

> 周期：本次技术债专项（静态审计 → 修复 → 回归 → 门禁）
> 结论：静态检查全绿、全量回归 715 passed / 5 skipped、修复 1 个必现 flaky 与若干真实缺陷

---

## 1. 现状审计（基线）

- 静态检查工具：`flake8`（含 `flake8-bugbear`、`flake8-comprehensions`），
  规则集 `F(pyflakes) / B(bugbear) / C4(comprehensions) / E9(语法)`。
- 基线问题：产线与测试合计约 **50+ 处**问题，集中在：
  - 未使用导入 / 未使用变量（F401/F841，约 40 处）；
  - 不必要列表推导 / `dict()` 构造（C4，约 6 处）；
  - 未绑定变量 `Any`（F821，真实缺陷，1 处）；
  - 循环变量未使用（B007，2 处）。
- 已知 flaky：`tests/test_project_state.py::test_file_change_detected_across_sessions`
  在本机文件系统 mtime 粗粒度下**必现失败**（同尺寸同 mtime 写入无法识别修改）。

## 2. 已修复问题

### 2.1 真实缺陷
| 位置 | 问题 | 修复 |
|------|------|------|
| `agent/tools/fileio.py` | 使用 `logging` 但未导入，执行审计路径会 NameError | 补 `import logging` |
| `tests/test_real_project_e2e.py:366` | `Dict[str, Any]` 中 `Any` 未导入（F821） | typing 导入补 `Any` |
| `agent/project_state.py` | 文件签名仅用 `(mtime_ns, size)`，同尺寸快速写入无法识别修改 | `scan()` 增加内容 `sha1`，比较逻辑优先内容签名、缺 sha1 时回退 mtime/size（兼容旧快照） |

### 2.2 静态卫生
- 清理产线 + 测试约 40 处 F401/F841 未使用导入/变量；
- C4 惯用法：`set(d for d in ...)` → set 推导、`dict(...)` → 字面量、
  `{s: 0 for s in ...}` → `dict.fromkeys`；
- B007 循环变量改下划线前缀；B042/B036 有意宽捕获补 `# noqa` 说明；
- 移除空 `except: raise`（`git_tool.py`）与冗余变量。

### 2.3 服务层（此前随方向三推进一并修复）
- `server/main.py` 移除模块级 `app = create_app()` 副作用，改为 `--factory` 启动；
- `server/tasks.py` 增加 `_running` 集合区分假 runner 取消；
- `server/main.py` SSE 对已结束任务补发 `done` 事件。

## 3. 安全审计结论

| 项 | 结论 |
|----|------|
| 路径穿越 | `resolve_workspace_path` 用 `.resolve()` + 工作区包含性校验；策略层拦截 NUL 字节与 `../`，`test_sandbox_security` 全绿 |
| 命令注入 | 终端用 `create_subprocess_exec` 显式 argv；Docker 模式在容器内执行；策略层拦截 sudo/rm -rf 等危险命令与受保护路径删除 |
| 网络策略 | deny / allowlist / allow + 假网络拦截；git push 归入网络命令遵守策略；请求 URL 审计 |
| MCP | 连接生命周期含 `AsyncExitStack` 清理与取消兜底；TLS 校验遵循 SDK（httpx 默认校验）；输出经 `output_truncate` 统一截断 |
| 敏感信息 | 全库检索未发现 API Key / Authorization 写入日志或 print 的路径；管理员初始 Key 仅启动时打印一次 |
| Web 服务 | 采用 API Key + Bearer 鉴权（非 Cookie），CSRF 面小；角色权限在 `require_role` 统一校验 |

## 4. 质量门禁

- 新增 `.flake8`（`select = F, B, C4, E9`，`jobs = 1`，忽略 E501）；
- 新增 `.github/workflows/quality-gate.yml`：对 `agent/ server/ swe_eval/ scripts/ tests/`
  的 push 自动跑 flake8 门禁（含 bugbear / comprehensions 插件）；
- 本地验证：`flake8 agent/ swe_eval/ server/ scripts/ tests/ --jobs=1` 全绿（exit 0）。

## 5. 回归验证

- 全量：`python -X utf8 -m pytest -q -p no:cacheprovider`
  → **715 passed / 5 skipped / 0 failed**（约 5.5 分钟）。
- 沙箱内运行受限说明：终端/子进程/内存熔断类测试需在沙箱外（或具备进程创建权限的
  环境）执行，否则报 `WinError 5 拒绝访问`（环境限制，非代码缺陷）。

## 6. 后续建议

- 接入 `mypy` / `coverage` 门禁，逐步提高类型覆盖与核心模块覆盖率；
- `tests/benchmarks/` 回归基准可继续扩容（把后续 SWE-bench 案例沉淀进来）；
- 保持 `docs/07-code-quality.md` 随每次技术债专项更新。
