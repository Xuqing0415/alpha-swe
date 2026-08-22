# 技能与插件

> 本文档从根 README 迁移（原「技能与插件（第 10 节）」），入口 `agent/context/`。

## 技能与插件（第 10 节）

### 插件动态注入（`agent/context/plugin.py`）
插件 = 纯上下文注入（Markdown + YAML front-matter），目录 `config/agent.yaml` 的 `plugin.dir`（默认 `./plugins/`），
按文件 mtime 热加载（新增/修改无需重启）。激活条件可组合叠加，命中任意一类即激活：
- `keywords`：任务指令关键词（如「数据库」→ `sql` 插件）；
- `file_ext`：项目/任务涉及文件扩展名（如 `.tsx` → `react-ts` 插件）；
- `project_file`：项目文件路径模式（fnmatch，如 `**/package.json`）；
- `project_dep`：项目依赖名（自动解析 `package.json` / `requirements.txt` / `pyproject.toml`）。

多插件同时命中时按 `priority` 降序注入，超出 `plugin.max_active` 截断（决策日志 `plugin.activate` / `plugin.truncate`）；
`config.active_plugins` 非空时作为白名单过滤。工作区扫描与指令路径提取由 `ProjectContext` 提供。

### 技能工作流（`agent/context/skill.py`）
技能 = 预定义子任务序列（YAML，目录 `config/agent.yaml` 的 `skills.dir`，默认 `./skills/workflows/`），
热加载；`skills.enabled` 由 `config/agent.yaml` 显式开启（程序化 `AppConfig()` 默认关闭，Worker Agent 恒关闭）。

```yaml
name: add-rest-endpoint
triggers:
  keywords: [rest, api, 端点, endpoint]
  file_ext: [.py, .ts]
steps:
  - name: route          # 展开为子任务：定义 REST 路由
    instruction: 定义 REST 端点路由
  - name: validation
    instruction: 编写请求参数校验
    dependencies: [route]
    on_failure: fallback     # 步骤失败决策点：fallback / abort / orchestrate
    fallback: 改用最简参数校验并重试
```

- **技能注册表（阶段二 2.1）**：每个技能可声明 `requires`（依赖技能）、`permissions`（所需工具权限）、
  `params`（参数定义）、`version` / `tags` / `author`；`skills/skill_manifest.json` 注册表按技能名补缺省元数据
  （YAML 显式键优先）；`validate()` 校验步骤依赖/`on_failure`/重复名/`when` 条件键；`discover()` 返回命中 +
  requires 依赖闭包 + 上下文建议；`record_usage()` / `usage_summary()` 记录使用与成败历史（版本管理，写入
  `skills.usage_log`）。决策日志：`skill.registry.loaded` / `skill.registry.invalid` / `skill.discovered`；
  展开时 `Task.metadata` 携带 `skill_version` / `requires` / `permissions` / `step_index` / `step_total`。
- **技能意图过滤（真实项目防误触发）**：`skills.require_task_intent: true`（默认）时，工作流激活要求任务指令命中
  `keywords` / `file_ext`；`project_dep` / `project_file` 单独命中仅在 `discover()` 中作为「上下文建议」返回，
  避免「把 README 翻译成英文」这类无关任务误展开 Express 工作流（决策日志 `skill.discovered` 区分两类命中）。
- 技能命中时由 `SkillLibrary.expand()` / `expand_pipeline()` 展开为 Task DAG（步骤依赖 -> 任务依赖），
  替代 LLM 规划器；多技能按给定顺序管道串联，前一个技能的最后一步链接到后一个技能的第一步（决策日志 `skill.pipeline`）；
- **步骤条件分支（阶段二 2.2）**：`SkillStep.when` 支持 `file_exists` / `not_file_exists` / `keyword` /
  `project_dep` / `always`（AND 语义），条件不满足的步骤跳过并记录 `skill.step_skip`；
  步骤决策点写入 `Task.metadata`：`on_failure=fallback` 时失败自动 `spawn` 回退任务（`skill.step_fallback`），
  `orchestrate` 时发出 `skill_intervention` 事件请求介入；
- 决策日志：`skill.activate` / `skill.expand` / `skill.pipeline` / `skill.step_skip` /
  `skill.step_fallback` / `skill.step_intervention`。

### 自然语言创建技能（阶段二 2.3，`agent/context/skill_author.py`）

- **确定性轨迹转换**：`SkillAuthor.from_trajectory()` 把已执行 Task 列表（含 `skill_step` 元数据）转换为技能
  YAML——步骤按顺序依赖，失败步骤自动标 `on_failure: fallback` 并带重试指令，触发器从技能名/描述自动提取关键词
  （含中文 2-3 字窗口）。
- **LLM 生成**：`SkillAuthor.from_llm()` 用 LLM 把自然语言/轨迹生成为更规范、带条件的技能 YAML（只接受
  ```yaml 代码块），失败自动回退确定性转换。
- **落盘热加载**：`SkillAuthor.save()` 写入技能库目录并立即用 `SkillLibrary` 校验，可被 `discover()` / `match()`
  马上发现（决策日志 `skill.authored` / `skill.saved`）。
- **入口**：`AgentLoop.save_skill(name, description)`（保存最近任务轨迹）与
  `AgentLoop.save_skill_from_natural_language(name, description, prompt)`（LLM 生成）；CLI 见 `scripts/save_skill.py`：
  ```powershell
  # trajectory.json: [{"step": "reproduce", "instruction": "复现问题", "outcome": "completed"}, ...]
  python -X utf8 scripts/save_skill.py --name fix-login-bug --description "修复登录失败" --trajectory trajectory.json
  python -X utf8 scripts/save_skill.py --name setup-env --description "初始化环境" --llm-prompt "把设置本地开发环境的步骤做成技能"
  ```

### 真实技能库与项目测试集（阶段二 2.4 验证）

- `skills/workflows/` 内置真实工作流：`add-rest-endpoint`、`bug-fix`（复现→定位→修复→回归）、
  `db-migration`（分析→生成→执行→回滚方案）、`test-generation`（分析→写用例→运行→修复失败）、
  `python-refactor`；`skills/skill_manifest.json` 注册表补齐 requires/permissions/params/tags。
- 基准集共 50 个可自动判定场景：`tests/test_benchmark_suite.py`（28 个黄金用例，L1-L4 分级，含规范解法与反例
  harness）、`tests/test_real_project_suite.py`（18 个真实技术栈场景：Express / Django+SQLAlchemy / pytest /
  Flask 迷你项目，验证技能发现、优先级排序、无关任务零误触发与 `AgentLoop` 端到端激活）、
  `tests/test_long_task_suite.py`（4 个长任务端到端：todo-crud / print-to-logging / fix-todos / loop-to-listcomp）。
- **技能执行进度可视化（阶段二 2.4）**：`task_start` 事件携带 `skill` / `skill_step` / `step_index` / `step_total`；
  TUI 任务树视图为技能步骤显示 `[技能 name::step i/N]` 徽标，底部状态栏与思维流同步显示当前技能进度
  （`skills_activated` 事件渲染为「技能工作流激活: ...（展开 N 个子任务）」）。

