# Textual TUI

> 本文档从根 README 迁移（原「TUI 多视图与用户干预」与「Textual TUI（第 14.1 节）」），入口 `tui/`。

### TUI 多视图与用户干预（`tui/app.py`）
- 纯终端 UI 设计（无 emoji / 无 256 色）：左栏任务面板（任务名 / 阶段 / 任务树 / 进度条 / 耗时，F6 可切换文件树）、
  主日志区（DataTable 三列虚拟滚动，`[HH:MM:SS] TYPE 内容` 八类语义色）、
  终端输出区（6 行，F3 全屏，D 键在原始输出与 diff 间切换）、底部状态栏（右对齐：tokens / round / mem / session）与输入栏；
- `F5` 轮换主区视图：主日志 / 文件变更（写操作渲染 unified diff）/ 监控（指标 + 告警）/ 时间线（span 耗时分布）；
- 窄屏（<100 列）自动降级为单栏（紧凑头 + 主日志），`F4` 手动宽/窄切换，`F2` 隐藏任务面板；
- 输入栏支持 `/pause` `/resume` `/status` `/retry` `/skip` `/quit` 命令与上下箭头历史；
- 高风险工具确认弹窗：命中 `agent.require_confirmation` 时弹出，支持
  `y`（批准一次）/ `a`（批准所有同类，写回 `loop._approve_rules`）/ `n`（拒绝）/
  `e:{"path":"..."}`（编辑参数后执行）；确认回调契约见 `tui/bridge.py` 的 `_on_confirmation`。

验证见 `tests/test_observability.py` 与 `tests/test_tui.py`。


## Textual TUI（第 14.1 节）

```bash
python -m tui "分析当前项目结构并给出改进建议"
python -m tui --config config/agent.yaml "修复失败的测试"
```

- **左栏任务面板**：任务名 / 阶段（颜色区分）/ 任务树（完成·进行>·等待·失败）/ 进度条 / 耗时；`F6` 切换为文件树视图（ASCII 连线、/ 过滤搜索、Enter 预览、* 标记最近修改、> 标记当前操作）；Agent 写入新文件后树自动增量刷新，新路径即时出现；
- **主日志区**：`[HH:MM:SS] TYPE 内容` 三列 DataTable 虚拟滚动（THINK 青 / ACT 亮白 / INFO 暗灰 /
  WARN 黄 / ERROR 红 / OK 绿），自动跟随 / 手动浏览模式，万级行流畅；
- **终端输出区**（6 行可滚动）：终端原始输出流（`TerminalTool` 逐行实时转发），`F3` 全屏；`D` 键切换为文件变更 diff（写前快照 unified diff）；
- **底部状态栏**（右对齐）：`tokens`（80% 变黄 / 95% 变红）、`round`（90% 变黄）、
  `mem`（记忆库用量 %）、`session`；
- **输入栏**：直接输入即注入高优先级指令；`/` 开头为命令（`/pause` `/resume` `/status`
  `/retry` `/skip` `/quit`），上下箭头浏览历史；
- **时间线视图**（`F5` 轮换到）：ASCII 火焰图展示 THINK/ACT/OBS span 的耗时分布，底部汇总总耗时 / 步数 / 最慢步骤；`上/下` 选中 span，`Enter` 弹出详情（参数 / 输出摘要 / 错误），`Esc` 关闭（`tui/timeline_view.py` + `Tracer.get_timeline_data()`）；
- **快捷键**：`F1` 帮助 / `F2` 任务面板 / `F3` 终端全屏 / `F4` 宽窄切换 / `F5` 主区视图（日志/变更/监控/时间线）/ `F6` 文件树 /
  `D` 输出与 diff 切换 / 时间线内 `上/下` 选中、`Enter` 详情 / `Ctrl+I` 注入 / `Ctrl+P` 暂停 / `Ctrl+R` 重试 / `Ctrl+S` 跳过 /
  `Ctrl+L` 清空终端 / `Tab` 切换窗格 / `q`、`Ctrl+C` 退出；
- 高风险操作（命中 `agent.require_confirmation`）弹出确认框：批准一次 / 批准所有同类 /
  拒绝 / 编辑参数后执行；会话结束后可用 `--replay` 按时间线回放档案。

实现要点：`AgentLoop.subscribe()` 实时事件订阅、`ExecutionContext.output_callback`
把命令输出逐行转发给右栏、Textual worker 在事件循环内跑 Agent 主循环
（`tui/bridge.py`、`tui/app.py`）。

