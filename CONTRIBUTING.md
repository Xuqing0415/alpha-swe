# 贡献指南

欢迎贡献！本指南说明如何搭建开发环境、编码规范与提交规范。

## 开发环境

- Python >= 3.12；推荐使用虚拟环境：
  ```powershell
  python -m venv .venv
  .venv\Scripts\Activate.ps1
  pip install -r requirements-server.txt
  pip install flake8 flake8-bugbear flake8-comprehensions pytest pytest-asyncio
  ```
- 运行测试（全量约 5 分钟，建议先跑相关单测）：
  ```powershell
  python -X utf8 -m pytest tests/ -q -p no:cacheprovider
  ```
- 代码检查：
  ```powershell
  flake8 agent/ swe_eval/ server/ scripts/ tests/ --jobs=1
  ```

## 代码规范

- 风格：`flake8`（`F/B/C4/E9`，见 `.flake8`）必须通过。
- 异步：核心执行链路使用 `asyncio`，工具统一继承 `agent.tools.base.Tool`。
- 配置：新增开关一律加到 `agent/config.py` 的 Pydantic 模型，示例同步到 `config/*.yaml`。
- 测试：新功能必须带离线单测；涉及外部工具（Docker/网络/子进程）的用例在沙箱内跳过。

## 提交规范（Conventional Commits）

- `feat(scope): 描述` —— 新功能（scope 示例：`multilang`、`tools`、`server`、`swebench`）
- `fix(scope): 描述` —— 缺陷修复
- `refactor(scope): 描述` —— 重构（不改行为）
- `docs(scope): 描述` —— 文档
- `test(scope): 描述` —— 测试
- `chore(scope): 描述` —— 构建/依赖/CI 等杂项

## 如何添加新语言支持

1. 在 `agent/code/language_parser.py` 注册扩展名到 `LANGUAGE_EXTS`；
2. 实现符号/调用边提取（优先 tree-sitter，必须带正则回退）；
3. 在 `tests/test_multilang.py` 添加样例断言；
4. 如该语言有测试框架，在 `agent/code/test_runner.py` 添加 framework 分支。

## 如何添加新工具

1. 在 `agent/tools/` 新建 `xxx_tool.py`，继承 `Tool`；
2. 在 `agent/config.py` 的 `ToolsConfig` 添加开关；
3. 在 `agent/core/loop.py` 的 `_default_tools()` 注册；
4. 沙箱策略若需拦截新工具，在 `agent/sandbox/policy.py` 扩展 `check()`。

## 分支与 PR

- 新分支建议前缀 `feat/` 或 `fix/`；提交前先跑相关单测与 flake8。
- PR 需通过 CI（`quality-gate.yml`：flake8 + pytest）才能合并。
