# 多语言支持与扩展工具

> 本文档从根 README 迁移（原「多语言支持与扩展工具（方向二）」）。
> 详细报告见 [../08-multilang-tools.md](../08-multilang-tools.md) 与 [../09-open-source-prep.md](../09-open-source-prep.md)。

## 多语言支持与扩展工具（方向二）

### 统一语法解析层（`agent/code/language_parser.py`）
- **11 种语言**：Python / JavaScript / TypeScript / Java / Go / Rust / C / C++ /
  C# / Ruby / PHP（24 个扩展名）；tree-sitter 优先、正则零依赖回退；统一产物
  `ParsedFile`（`symbols` / `calls` / `imports`）。
- **AST 摘要泛化**（`ast_summary.py`）：`CODE_EXTS` 升级为 `ALL_CODE_EXTS`，
  `_summarize_generic()` 接入多语言；调用图（`call_graph.py::_index_generic`）与
  文件推荐（`recommend.py`）同步覆盖新扩展名。
- **测试运行器**（`test_runner.py`）：Maven / Gradle / cargo / CTest 的 argv 构造与
  输出解析（保留 pytest / jest / go test）。
- **依赖管理**（`agent/code/dependency_manager.py`）：识别 pom.xml / build.gradle /
  go.mod / Cargo.toml / CMakeLists.txt / vcpkg.json / package.json / requirements.txt /
  pyproject.toml / Pipfile，提供依赖总览与审计命令建议。

### 新增工具（`agent/tools/`，已注册进 `agent/core/loop.py::_default_tools`）
- `database`（`database_tool.py`）：SQLite / PostgreSQL / MySQL；默认只读，
  写操作需 `read_only: false` + `confirm: true` + `allow_write` 配置；
- `dependency`（`dependency_tool.py`）：`report` 扫描清单、`audit` 只返回审计命令建议（不联网执行）；
- `cloud`（`cloud_tool.py`）：aws / kubectl / docker / gcloud / az 封装，默认关闭
  （`tools.cloud.enabled`），未确认一律拒绝、危险子命令拦截。

### VS Code 扩展 MVP（`vscode-extension/`）
选中代码 + 自然语言指令 → 提交 `POST /api/v1/tasks` → 轮询状态 → 完成后打开工作区
`git diff`；配置 `alphaSwe.baseUrl` / `alphaSwe.apiKey` / `alphaSwe.timeout`，
命令 **Alpha-SWE: 对选中代码执行自然语言指令**（`Ctrl+Alt+S`）。详见 `../../vscode-extension/README.md`。

验证：`tests/test_multilang.py`（多语言解析 / 测试运行器 / 依赖 / 新工具，19 项）；
详细报告见 `../08-multilang-tools.md` 与 `../09-open-source-prep.md`。

