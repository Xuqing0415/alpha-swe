# 目标驱动（Goal）

> 入口：`agent/core/goal.py`（模型与台账）、`agent/core/loop.py`（运行链路）。
> 默认关闭；只有显式 `--goal` / `--goal-id` 或配置 `agent.goal_enabled: true` 时启用。

## 1. 目标与任务的关系

Goal 描述“要达成什么”，Task 描述“这一轮具体做什么”。启用目标驱动后：

1. `Goal.to_tasks()` 把根目标或未终结子目标转换为确定性 Task；
2. Planner/执行器按 Task DAG 执行；
3. 每个 Task 终结（完成 / 失败 / 跳过）后回写对应目标节点；
4. 子目标全部终结后重新计算父目标进度并收敛状态。

无子目标时，根目标本身即一个任务。目标台账默认位于
`<workspace>/.swe-agent/goals.json`，写入采用临时文件 + 原子替换；台账损坏时
回退为空台账，不阻断任务。

## 2. 状态机

```text
pending -> approved -> in_progress -> completed
                           |              ^
                           +-> failed ----+ (允许 failed -> in_progress 重试)
```

`completed` 为终态。目标块以 `[当前目标]` 注入 System Prompt，包含描述、状态、
进度与子目标列表，降低长任务中的目标漂移。

## 3. CLI

```powershell
# 创建目标，并声明顺序子目标
python -X utf8 -m agent run "交付登录功能" --goal `
  --subgoal "实现登录接口" --subgoal "补充回归测试"

# 继续既有目标
python -X utf8 -m agent run "继续推进" --goal-id goal_ab12cd34ef
```

约束：

- `--goal` 与 `--goal-id` 互斥；`--subgoal` 只能与 `--goal` 同用；
- `--goal` / `--goal-id` 不能与 `--dry-run` 同用，因为目标入口会写台账，
  而 dry-run 承诺零写入；
- 用法错误返回退出码 `2`。

## 4. TUI

```powershell
python -X utf8 -m tui --goal "交付登录功能"
```

- `F8` 在左侧面板切换「任务 / 目标」视图；
- 目标视图显示根目标、进度条、子目标状态与台账路径；
- 状态栏左侧同步显示当前视图名。

## 5. 空闲续规划

任务队列清空时，如果仍有活跃目标：

- 未终结子目标会生成下一批 Task；
- 子目标全部终结但父目标未收敛时执行收口；
- 单目标尚未收敛时最多生成 `goal_idle_continue_limit` 次收敛任务。

每次续规划记录 `goal.idle_continue` 决策日志与 `goal_idle_continue` 事件。
上限用于防止“无进展但不断规划”的死循环。

## 6. 失败语义

- 子目标任务失败：对应子目标转 `failed`，根目标随之转 `failed`；
- 根目标失败后可在后续运行通过 `--goal-id` 重新激活（`failed -> in_progress`）；
- 目标回写异常只记录警告，不影响任务本身收尾。

## 7. 相关事件

| 事件 | 含义 |
|------|------|
| `goal_created` | 新目标写入台账 |
| `goal_updated` | 目标状态 / 进度变化 |
| `goal_plan` | 由目标生成任务 DAG |
| `goal_idle_continue` | 队列清空后的续规划 |

事件统一归入 `EventKind.GOAL`，详见 [../event-schema.md](../event-schema.md)。

验证：`tests/test_goal.py`、`tests/test_goal_wiring.py`。
