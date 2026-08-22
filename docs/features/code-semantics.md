# 代码语义理解层

> 本文档从根 README 迁移（原「代码语义理解层（阶段一）」），入口 `agent/code/`。

## 代码语义理解层（阶段一）

让 Agent 拥有程序员级别的代码感知能力，入口 `agent/code/`：

- **AST 感知读取**（`ast_summary.py`）：`file_ops` 读取代码文件时除原文外附加结构摘要——类/函数/方法签名、
  参数、装饰器、导入与导出符号；Python 用标准库 `ast`，JS/TS 优先 tree-sitter 精确提取，
  未安装时回退正则近似（决策日志 `symbol.retrieved`）。
- **调用图**（`call_graph.py`）：项目启动时构建 `caller -> callee` 有向边（Python `ast`、JS/TS tree-sitter），
  修改符号前可查"谁调用了它 / 它调用了谁"（`callers_of` / `callees_of`），
  FileIO 读取时注入影响范围，决策日志 `call_graph.hit` / `call_graph.indexed`。
- **项目约定提取**（`project_profile.py`）：扫描 `package.json` / `pyproject.toml` / `tsconfig.json` 等，
  自动识别技术栈（含版本）、lint/严格模式约定、顶层目录结构，作为持久化上下文注入 System Prompt
  （决策日志 `profile.injected`）。
- **Planner 注入**：`planner.plan(..., call_graph, project_context)` 把高频符号影响面与项目约定
  注入拆分提示，让子任务拆分考虑连带修改（决策日志 `planner.call_graph.injected`）。

安装 tree-sitter（可选，未安装自动回退正则）：
```powershell
pip install tree-sitter tree-sitter-javascript tree-sitter-typescript
```

验证：`tests/test_code_semantics.py`（AST 摘要 / 调用图 / 项目约定 / 工具与 Prompt 注入 / tree-sitter 与回退）。

