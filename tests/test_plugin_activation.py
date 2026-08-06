"""阶段三测试：插件动态激活（关键词/文件类型/项目依赖/优先级/白名单/热加载）。"""
import json
from pathlib import Path

from agent.context.plugin import PluginManager, ProjectContext
from agent.core.decision_logger import DecisionLogger


def write_plugin(d: Path, name: str, body: str) -> Path:
    p = d / f"{name}.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_keyword_trigger(ws_tmp):
    d = ws_tmp / "plugins"
    d.mkdir()
    write_plugin(d, "sql", """---
name: sql
priority: 7
triggers:
  keywords: [数据库, sql, 索引]
---
# SQL 规范
- 使用参数化查询
""")
    pm = PluginManager(plugins_dir=str(d))
    active = pm.get_active("给用户表加索引，优化数据库查询")
    assert [p.name for p in active] == ["sql"]


def test_file_ext_trigger(ws_tmp):
    d = ws_tmp / "plugins"
    d.mkdir()
    write_plugin(d, "react-ts", """---
name: react-ts
priority: 6
triggers:
  file_ext: [.tsx]
---
# React+TS 规范
- 函数组件 + Hooks
""")
    pm = PluginManager(plugins_dir=str(d))
    active = pm.get_active("实现登录页", files=["src/pages/Login.tsx"])
    assert [p.name for p in active] == ["react-ts"]
    # 没有 .tsx 文件时不激活
    assert pm.get_active("实现登录页", files=["src/pages/Login.java"]) == []


def test_project_dep_trigger_via_package_json(ws_tmp):
    d = ws_tmp / "plugins"
    d.mkdir()
    write_plugin(d, "express", """---
name: express
priority: 5
triggers:
  project_dep: [express]
---
# Express 规范
- 中间件按职责拆分
""")
    (ws_tmp / "package.json").write_text(
        json.dumps({"dependencies": {"express": "^4.19.0"}}), encoding="utf-8"
    )
    pm = PluginManager(plugins_dir=str(d))
    pc = ProjectContext.scan(str(ws_tmp), max_depth=1)
    assert "express" in pc.deps
    active = pm.get_active("添加一个 /health 接口", files=pc.files, deps=pc.deps)
    assert [p.name for p in active] == ["express"]


def test_multi_plugin_priority_and_truncate(ws_tmp):
    d = ws_tmp / "plugins"
    d.mkdir()
    write_plugin(d, "low", """---
name: low
priority: 1
triggers:
  keywords: [数据库]
---
# low
""")
    write_plugin(d, "high", """---
name: high
priority: 9
triggers:
  keywords: [数据库]
---
# high
""")
    pm = PluginManager(plugins_dir=str(d), max_active=2)
    active = pm.get_active("处理数据库查询")
    assert [p.name for p in active] == ["high", "low"]

    pm2 = PluginManager(plugins_dir=str(d), max_active=1)
    assert [p.name for p in pm2.get_active("处理数据库查询")] == ["high"]


def test_whitelist_filters_plugins(ws_tmp):
    d = ws_tmp / "plugins"
    d.mkdir()
    write_plugin(d, "sql", """---
name: sql
triggers:
  keywords: [数据库]
---
# SQL
""")
    write_plugin(d, "react", """---
name: react
triggers:
  keywords: [数据库]
---
# React
""")
    pm = PluginManager(plugins_dir=str(d), whitelist=["sql"])
    active = pm.get_active("处理数据库查询")
    assert [p.name for p in active] == ["sql"]


def test_decision_log_records_activation_and_truncate(ws_tmp):
    d = ws_tmp / "plugins"
    d.mkdir()
    write_plugin(d, "a", """---
name: a
priority: 3
triggers:
  keywords: [数据库]
---
# A
""")
    write_plugin(d, "b", """---
name: b
priority: 1
triggers:
  keywords: [数据库]
---
# B
""")
    dl = DecisionLogger()
    pm = PluginManager(plugins_dir=str(d), max_active=1, decision_logger=dl)
    pm.get_active("处理数据库查询")
    names = [dp.name for dp in dl.decisions]
    assert "plugin.activate" in names
    assert "plugin.truncate" in names
    trunc = [dp for dp in dl.decisions if dp.name == "plugin.truncate"][0]
    assert "b" in trunc.decision


def test_hot_reload_new_plugin(ws_tmp):
    d = ws_tmp / "plugins"
    d.mkdir()
    pm = PluginManager(plugins_dir=str(d))
    assert pm.list_plugins() == []
    write_plugin(d, "newbie", """---
name: newbie
triggers:
  keywords: [热加载]
---
# New
""")
    active = pm.get_active("热加载测试")
    assert [p.name for p in active] == ["newbie"]


def test_project_context_from_instruction_merges_files(ws_tmp):
    base = ProjectContext(files=["app/models.py"], deps={"flask"})
    pc = ProjectContext.from_instruction("修复 src/utils.py 与 app/main.py 的 bug", base=base)
    assert "src/utils.py" in pc.files
    assert "app/main.py" in pc.files
    assert "app/models.py" in pc.files
    assert "flask" in pc.deps