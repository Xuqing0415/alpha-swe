# Alpha-SWE 旧版七层原型（legacy）

> [!WARNING] 仅供对照参考，不再演进。请勿在此目录上开发新功能；新用户请使用
> `python -m agent run "任务"`（新核心，配置 `config/agent.yaml`）。

## 这是什么

早期“七层进化”原型（MemoryBank / 多 Agent / Background / PluginLoader / Compressor /
Sandbox / MCP+TUI）的完整实现，保留用于对照设计与验证。与 `agent/` 新核心互不依赖，
入口与默认配置均不同。

## 入口

```powershell
# 从仓库根目录运行（配置按 legacy/config.yaml 解析）
python legacy/main.py -m demo
python legacy/main.py -m multi_agent
python legacy/main.py -i

# 旧版集成自检
cd legacy
python -X utf8 -m pytest test_all.py -v
```

## 目录

- `main.py` — 七层系统主入口（standard / multi_agent / demo / interactive）
- `loop.py`、`scheduler.py`、`executor*.py`、`parser.py` — 执行链
- `memory_bank.py`、`compressor.py`、`plugin_loader.py`、`sandbox.py` — 记忆/压缩/技能/沙箱
- `platform_cmds.py`、`terminal_ui.py`、`structured_log.py`、`event_bus.py`、`recovery.py` 等
- `tools/` — 旧版工具实现（与新核心 `agent/tools/` 无关）
- `config.yaml` — 旧版配置（无 Pydantic 校验）
