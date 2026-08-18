# 08 · 多语言支持与扩展工具报告

> 周期：方向二（扩展语言/工具）+ 方向三（开源准备）并行推进
> 结论：多语言解析/测试/依赖链路已落地（正则回退零新依赖，tree-sitter 可选增强）；
> 新增数据库、依赖、云 CLI 三个工具并接入主循环；VS Code 扩展 MVP 编译通过

---

## 1. 统一语法解析层（`agent/code/language_parser.py`）

- **支持语言**：Python / JavaScript / TypeScript / Java / Go / Rust / C / C++ /
  C# / Ruby / PHP，共 11 种；`LANGUAGE_EXTS` 覆盖 24 个扩展名。
- **解析策略**：优先 tree-sitter（`_parse_with_tree_sitter`，grammar 缺失时自动
  跳过），未安装一律回退正则（`_parse_regex`）——零硬依赖，CI/离线环境可用。
- **统一产物** `ParsedFile`：
  - `symbols`：`LangSymbol(name, kind, line, args, end_line)`，kind 覆盖
    function / method / class / interface / enum / struct / trait / module / type；
  - `calls`：`(owner, callee)` 调用边，按字节偏移归属到最内层符号行区间
    （末符号行区间延伸到文件末尾，避免尾部调用丢失）；
  - `imports`：import / use / include / require / using 等导入条目。
- **防误报**：`_CALL_STOPWORDS`（if/for/return/assert…）、`_KEYWORD_PREFIX`
  （return/throw/new… 前缀过滤调用语句）、`new Foo(` 构造调用排除、去重键
  `(name, kind, line)`。
- **验证**：`tests/test_multilang.py` 19 项（各语言符号/调用/导入 + 工具 + 依赖）。

## 2. AST 摘要与调用图泛化

- `agent/code/ast_summary.py`：`CODE_EXTS` 升级为 `ALL_CODE_EXTS`，新增
  `_summarize_generic()` 把多语言符号摘要接入 `summarize_file()`；
  `is_code_file()` 同步覆盖新扩展名。
- `agent/code/call_graph.py`：`_index_generic()` 复用 `language_parser` 构建
  `defs / calls / reverse`；`agent/code/recommend.py` 文件后缀集扩展。

## 3. 测试运行器扩展（`agent/code/test_runner.py`）

- `_build_argv(framework, target, verbose, workspace)` 新增四个框架：
  - `maven`：`mvn -f <pom> -q test`；`gradle`：`gradle test`（可选 `-p <workspace>`）；
  - `cargo`：`cargo test`；`ctest`：`ctest --output-on-failure -R <target>`；
  - 保留 pytest / jest / go test 原有分支。
- `parse_test_output()` 新增 Maven（Surefire 摘要）、Gradle、cargo（failures 摘要）、
  CTest（FAILED 列表）、go（`--- FAIL`）解析，输出 `TestFailure(name, reason)`。

## 4. 依赖管理（`agent/code/dependency_manager.py`）

- 清单识别：`pom.xml` / `build.gradle` / `go.mod` / `Cargo.toml` /
  `CMakeLists.txt` / `vcpkg.json` / `package.json` / `requirements.txt` /
  `pyproject.toml` / `Pipfile`。
- `parse_manifest()` 输出 `(language, manager, path, entries)`；`dependency_report()`
  生成含依赖名的文本总览（单个清单超过 20 项截断展示）；`audit_command()`
  返回各生态审计命令建议（pip-audit / npm audit / cargo audit / mvn 等）。
- **验证**：`tests/test_multilang.py`（go.mod/Cargo.toml/requirements 报告、
  pom.xml 命名空间兼容、package.json）。

## 5. 新增工具（接入 `agent/core/loop.py::_default_tools`）

| 工具 | 模块 | 行为与安全 |
|------|------|-----------|
| `database` | `agent/tools/database_tool.py` | SQLite / PostgreSQL / MySQL；默认只读；写操作需 `read_only: false` + `confirm: true` + `allow_write` 配置 |
| `dependency` | `agent/tools/dependency_tool.py` | `report` 扫描清单；`audit` 只返回命令建议，不自动联网执行 |
| `cloud` | `agent/tools/cloud_tool.py` | 封装 aws / kubectl / docker / gcloud / az；默认关闭（`tools.cloud.enabled`），未确认一律拒绝，危险子命令（`s3 rb --force` 等）拦截 |

- `agent/config.py` 的 `ToolsConfig` 新增 `database / dependency / cloud` 三组开关；
  cloud 仅在显式开启时注册（决策日志 `cloud.deny.*`）。
- **验证**：`tests/test_multilang.py`（sqlite 只读查询、写操作拒绝、audit 建议、
  云工具确认与危险子命令拦截）。

## 6. VS Code 扩展 MVP（`vscode-extension/`）

- 功能：选中代码 + 自然语言指令 → `POST /api/v1/tasks`（Bearer Token 鉴权）→
  轮询 `GET /api/v1/tasks/{id}` → 取消走 `POST /api/v1/tasks/{id}/cancel` →
  完成后打开工作区 `git diff` 视图。
- 配置：`alphaSwe.baseUrl`（默认 `http://127.0.0.1:8000`）、`alphaSwe.apiKey`、
  `alphaSwe.timeout`；命令 **Alpha-SWE: 对选中代码执行自然语言指令**（`Ctrl+Alt+S`）。
- 已验证：`npm install && npm run compile`（tsc 严格模式）通过；`node_modules/`
  `out/` `*.vsix` 已加入 `.gitignore`。
- 说明：MVP 聚焦“选中→指令→diff 展示”主链路；历史任务、SSE 实时进度、多工作区
  留待迭代。

## 7. 回归验证

- `tests/test_multilang.py`：19 passed；
- 受影响回归：`test_tool_registration.py` / `test_code_semantics.py` /
  `test_recommend_files.py` / `test_swe_eval_optimization.py`：32 passed / 2 skipped；
- 全量 pytest 与 flake8 门禁见提交时验证记录。
