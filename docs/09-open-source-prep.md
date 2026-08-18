# 09 · 开源准备与社区基础报告

> 周期：方向三（开源与社区建设）第一阶段
> 结论：仓库已具备公开条件（许可证/贡献指南/行为准则/变更日志/路线图 + CI 门禁）；
> 公开发布、社区运营等对外操作按约定由用户自行执行

---

## 1. 代码清理与规范化

- 移除内部调试文件（`_dbg_lp.py` 等），`git status` 无敏感/临时文件残留；
- 代码风格门禁沿用 `.flake8`（`F/B/C4/E9`，`jobs=1`），CI 对 push 自动执行；
- 旧版七层原型保留但明确标注“仅供对照参考，不再演进”（README「两套架构」小节）。

## 2. 许可证与元数据

| 文件 | 说明 |
|------|------|
| `LICENSE` | MIT 许可证 |
| `CONTRIBUTING.md` | 开发环境、代码规范、提交规范（Conventional Commits）、如何添加语言/工具 |
| `CODE_OF_CONDUCT.md` | 贡献者行为准则 |
| `CHANGELOG.md` | 语义化版本（当前 0.1.0）变更记录 |
| `ROADMAP.md` | v0.2 多语言与工具增强（本批已完成项已勾选）、v0.3 插件生态、v0.4 可观测性 |

## 3. CI/CD（`.github/workflows/quality-gate.yml`）

- `lint` job：flake8（含 bugbear / comprehensions）；
- `test` job：全量 pytest（`python -X utf8 -m pytest tests -q -p no:cacheprovider`），
  忽略 chaos / soak / fault / benchmark / e2e 等长时或外部依赖用例（
  `--ignore=tests/test_chaos_smoke.py --ignore=tests/test_soak.py --ignore=tests/test_fault_injection.py
  --ignore=tests/test_benchmark_suite.py --ignore=tests/test_real_project_e2e.py`），
  Python 3.12 镜像，pytest 缓存清理 + 失败重试 2 次；
- 分支保护、PR 必须过 CI 等 GitHub 侧设置在公开发布时由用户配置。

## 4. 版本管理

- 基线版本 `0.1.0`（`CHANGELOG.md` 记录 2026-08 各方向里程碑）；
- 提交遵循 Conventional Commits：`feat(scope)` / `fix(scope)` / `docs(scope)` 等。

## 5. 后续（用户自行执行）

- 推送到 GitHub 公开仓库、设置标签与分支保护；
- 发布公告 / 社区运营（Issues、Discussions）；
- 插件索引仓库与生态激励（对应 ROADMAP v0.3）。
