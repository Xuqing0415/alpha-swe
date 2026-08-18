# TaskBoard

轻量 JSON 任务看板库 + CLI。零外部依赖（仅标准库），测试用 pytest。

## 模块

- `taskboard/models.py` — Task 数据类、Priority/Status 枚举
- `taskboard/store.py` — JsonStore 原子持久化
- `taskboard/board.py` — Board 增删改查/搜索/过滤/统计
- `taskboard/cli.py` — argparse CLI（add/list/complete/delete）
- `taskboard/utils.py` — slugify/日期解析/组合过滤

## 使用

```bash
python -m taskboard.cli --db tasks.json add "写周报" --priority high --tags work
python -m taskboard.cli --db tasks.json list
python -m taskboard.cli --db tasks.json complete <id>
```

## 测试

```bash
cd tests/benchmarks/sample_project
python -m pytest
```
