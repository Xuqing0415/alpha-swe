# -*- coding: utf-8 -*-
"""真实项目长期任务实战执行器（方向一·阶段 3）。

用真实 LLM（默认 DeepSeek）在基准项目 tests/benchmarks/sample_project 上
执行 L1-L4 共 8 个任务，每个任务独立工作区：

1. 复制基准项目到 task_workspace/<task_id>；
2. 子进程运行 `python -m agent run "<任务>" --config config/benchmark.yaml
   --workspace <ws> --output json --timeout N`；
3. 在任务工作区运行 pytest + 按任务的完成标准校验；
4. 汇总指标（完成率/耗时/token/重试/回归率）到 logs/real_project_report.json。

用法:
    python -X utf8 scripts/run_real_project_tasks.py [--tasks 1,2] [--docker]
        [--timeout 900] [--max-cost 1.0] [--skip-llm]

--skip-llm: 跳过真实 LLM 执行，只重建工作区并验证完成标准（CI 回归用）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PROJECT = REPO_ROOT / "tests" / "benchmarks" / "sample_project"
TASK_WORKSPACE_ROOT = REPO_ROOT / "test_workspace" / "real_project_tasks"
REPORT_PATH = REPO_ROOT / "logs" / "real_project_report.json"
CONFIG_PATH = REPO_ROOT / "config" / "benchmark.yaml"


def _rmtree(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for name in list(files) + list(dirs):
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        ".pytest_tmp", "__pycache__", ".pytest_cache", ".git",
        "pytest-cache-files-*"))


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


# ---------------- 任务定义 ----------------
@dataclass
class BenchTask:
    id: str
    level: str
    prompt: str
    check: Callable[[Path], List[str]]   # 返回失败原因列表（空=通过）
    note: str = ""


def _pytest_ok(ws: Path) -> bool:
    # 从仓库根运行、传绝对路径：强制把基准项目工作区作为 pytest 根目录/
    # 配置边界，避免被仓库根 pytest.ini 的 norecursedirs（test_workspace）
    # 排除导致 0 收集。注：Windows 上 cwd 与路径同目录时 pytest 会把绝对
    # 路径转成相对路径导致 --confcutdir 校验失败，故不设 cwd。
    ws = Path(ws).resolve()
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
           "--rootdir", str(ws), "--confcutdir", str(ws),
           str(ws / "tests")]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=120, encoding="utf-8", errors="replace")
    # pytest 会在 rootdir 下留下 pytest-cache-files-* 临时目录，跑完即清理
    for name in list(ws.iterdir()):
        if name.name.startswith("pytest-cache-files-"):
            _rmtree(name)
    return proc.returncode == 0


def _has_content(p: Path, *needles: str, absent: bool = False) -> bool:
    text = _read_text(p)
    if absent:
        return all(n not in text for n in needles)
    return all(n in text for n in needles)


TASKS: List[BenchTask] = [
    BenchTask(
        id="L1-01",
        level="L1",
        prompt=("taskboard/utils.py 的 slugify 函数有一个未使用的 import "
                "（string 模块），以及一个不规范的单字母变量名 l。请移除 "
                "未使用的 import，并把 l 改名为更有意义的变量名。不要改变 "
                "函数行为，保持所有测试通过。"),
        note="移除未使用 import + 变量改名",
        check=lambda ws: (
            [] if _pytest_ok(ws) else ["pytest 未通过"]
        ) + ([] if not _has_content(ws / "taskboard/utils.py", "import string")
             else ["utils.py 仍包含 import string"])
        + ([] if _has_content(ws / "taskboard/utils.py", "l =") is False
           else ["utils.py 仍存在单字母变量 l"]),
    ),
    BenchTask(
        id="L1-02",
        level="L1",
        prompt=("taskboard/models.py 中 Task 类的 total_estimate 字段注释把 "
                "单位写错了：注释说“小时”，但代码语义是“分钟”（board.stats() "
                "按分钟统计）。请修正该字段的 docstring，使单位说明与代码一致，"
                "保持所有测试通过。"),
        note="修正 docstring 单位",
        check=lambda ws: (
            [] if _pytest_ok(ws) else ["pytest 未通过"]
        ) + ([] if "分钟" in _read_text(ws / "taskboard/models.py")
             else ["models.py 的 total_estimate 注释未改为分钟"])
        + ([] if "小时" not in _read_text(ws / "taskboard/models.py")
           else ["models.py 仍出现错误单位“小时”"]),
    ),
    BenchTask(
        id="L2-03",
        level="L2",
        prompt=("taskboard/board.py 的 Board.search() 目前是大小写敏感匹配："
                "搜索 “login” 找不到标题含 “Login” 的任务。请改为大小写不敏感"
                "匹配，并在 tests/test_board.py 中添加一个覆盖该行为的测试用例。"),
        note="修复大小写敏感 bug + 回归测试",
        check=lambda ws: (
            [] if _pytest_ok(ws) else ["pytest 未通过"]
        ) + ([] if (_has_content(ws / "taskboard/board.py", ".lower()")
                    or _has_content(ws / "taskboard/board.py", ".casefold()"))
             else ["search() 未改为大小写不敏感"])
        + ([] if "case" in _read_text(ws / "tests/test_board.py").lower()
           or "Login" in _read_text(ws / "tests/test_board.py")
           else ["未添加大小写测试用例"]),
    ),
    BenchTask(
        id="L2-04",
        level="L2",
        prompt=("taskboard/board.py 的 Board.add() 缺少参数校验：空标题应该被"
                "拒绝（抛 ValueError）；tags 应为字符串列表。请为 add() 添加 "
                "标题非空校验，并在 tests/test_board.py 中添加空标题抛异常与"
                "正常添加的测试。"),
        note="添加参数校验 + 测试",
        check=lambda ws: (
            [] if _pytest_ok(ws) else ["pytest 未通过"]
        ) + ([] if _has_content(ws / "taskboard/board.py", "ValueError")
             else ["add() 未添加校验"])
        + ([] if "空标题" in _read_text(ws / "tests/test_board.py")
           or "empty" in _read_text(ws / "tests/test_board.py").lower()
           else ["未添加空标题测试"]),
    ),
    BenchTask(
        id="L3-05",
        level="L3",
        prompt=("taskboard/board.py 的 update() 方法里有一行占位代码 "
                "“task.updated_at = task.updated_at”，并没有真正更新时间戳。"
                "请修复：在 taskboard/models.py 的 Task 类新增 touch() 方法用于"
                "刷新 updated_at，并让 board.py 的 update() 与 complete() 调用它"
                "（跨文件修改，保持 API 兼容）。"),
        note="跨文件重构：Task.touch() + update()/complete() 调用",
        check=lambda ws: (
            [] if _pytest_ok(ws) else ["pytest 未通过"]
        ) + ([] if _has_content(ws / "taskboard/models.py", "def touch")
             else ["models.py 缺少 Task.touch()"])
        + ([] if "touch()" in _read_text(ws / "taskboard/board.py")
           or ".touch()" in _read_text(ws / "taskboard/board.py")
           else ["board.py 未调用 touch()"]),
    ),
    BenchTask(
        id="L3-06",
        level="L3",
        prompt=("taskboard/board.py 的 filter_by() 与 taskboard/utils.py 的 "
                "filter_tasks() 功能重复。请让 Board.filter_by() 委托给 "
                "utils.filter_tasks()（保持 filter_by 的签名与返回类型不变），"
                "移除重复实现，并确保现有测试全部通过。"),
        note="跨模块重构：filter_by 委托 filter_tasks",
        check=lambda ws: (
            [] if _pytest_ok(ws) else ["pytest 未通过"]
        ) + ([] if _has_content(ws / "taskboard/board.py",
                                "filter_tasks", "from taskboard.utils import")
             else ["board.py 未委托给 utils.filter_tasks"]),
    ),
    BenchTask(
        id="L4-07",
        level="L4",
        prompt=("为 taskboard CLI 新增 stats 子命令：输出 JSON 形式的看板统计"
                "（复用 Board.stats() 的返回结构），并在 tests/test_cli.py 中"
                "添加对应测试（含 --db 参数）。"),
        note="新增 CLI 子命令 + 测试",
        check=lambda ws: (
            [] if _pytest_ok(ws) else ["pytest 未通过"]
        ) + ([] if _has_content(ws / "taskboard/cli.py", "stats")
             else ["cli.py 缺少 stats 子命令"])
        + ([] if _has_content(ws / "tests/test_cli.py", "stats")
           else ["未添加 stats 的 CLI 测试"]),
    ),
    BenchTask(
        id="L4-08",
        level="L4",
        prompt=("为 Board 新增 find_by_tags(tags) 方法：接受标签列表，返回标题"
                "包含任一给定标签的任务（去重、保持原有顺序）；并在 "
                "tests/test_board.py 中添加测试。"),
        note="新增 Board API + 测试",
        check=lambda ws: (
            [] if _pytest_ok(ws) else ["pytest 未通过"]
        ) + ([] if _has_content(ws / "taskboard/board.py", "def find_by_tags")
             else ["board.py 缺少 find_by_tags()"])
        + ([] if _has_content(ws / "tests/test_board.py", "find_by_tags")
           else ["未添加 find_by_tags 测试"]),
    ),
]


# ---------------- 执行 ----------------
def run_task(task: BenchTask, docker: bool, timeout: int, max_cost: float,
             skip_llm: bool) -> Dict[str, object]:
    ws = TASK_WORKSPACE_ROOT / task.id
    if ws.exists():
        _rmtree(ws)
    _copy_tree(SAMPLE_PROJECT, ws)
    record: Dict[str, object] = {
        "id": task.id, "level": task.level, "note": task.note,
        "prompt": task.prompt, "workspace": str(ws),
    }
    if skip_llm:
        # 只验证完成标准（不消耗 LLM）
        record["llm_skipped"] = True
        fails = task.check(ws)
        record["pytest_ok"] = not fails
        record["check_fails"] = fails
        record["pass"] = not fails
        return record

    started = time.time()
    cmd = [
        sys.executable, "-X", "utf8", "-m", "agent", "run",
        task.prompt,
        "--config", str(CONFIG_PATH),
        "--workspace", str(ws),
        "--output", "json",
        "--timeout", str(timeout),
    ]
    if max_cost and max_cost > 0:
        cmd += ["--max-cost", str(max_cost)]
    if docker:
        cmd += []  # benchmark.yaml 默认 docker_enabled=false；--docker 用专属配置
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout + 60, cwd=str(REPO_ROOT),
                              encoding="utf-8", errors="replace")
        raw = proc.stdout.strip()
        payload = {}
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                # CLI 输出为多行美化 JSON；若前置有日志杂讯则从第一个 { 截取
                try:
                    start = raw.index("{")
                    payload = json.loads(raw[start:])
                except (ValueError, json.JSONDecodeError):
                    payload = {"_parse_failed": raw[-800:]}
        record.update(payload)
        record["exit_code"] = proc.returncode
    except subprocess.TimeoutExpired:
        record["exit_code"] = -9
        record["error"] = "agent 子进程超时"
    record["wall_s"] = round(time.time() - started, 1)

    # 完成标准验证
    fails = task.check(ws)
    record["pytest_ok"] = not fails
    record["check_fails"] = fails
    record["pass"] = (proc.returncode if "exit_code" in record
                      and record["exit_code"] != -9 else -9) in (0,) and not fails
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description="真实项目实战执行器")
    ap.add_argument("--tasks", default="", help="逗号分隔任务 id（默认全部）")
    ap.add_argument("--docker", action="store_true",
                    help="使用 Docker 沙箱（需 docker 配置）")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--max-cost", type=float, default=0.0)
    ap.add_argument("--skip-llm", action="store_true",
                    help="只重建工作区并验证完成标准（不调 LLM）")
    args = ap.parse_args()

    selected = [t for t in TASKS
                if not args.tasks or t.id in args.tasks.split(",")]
    TASK_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    results = []
    for t in selected:
        print(f"\n===== 执行任务 {t.id} [{t.level}] {t.note} =====", flush=True)
        rec = run_task(t, args.docker, args.timeout, args.max_cost,
                       args.skip_llm)
        results.append(rec)
        print(f"  pass={rec.get('pass')} exit={rec.get('exit_code')} "
              f"elapsed={rec.get('elapsed_s')} tokens={rec.get('tokens')} "
              f"check={rec.get('check_fails')}", flush=True)

    passed = sum(1 for r in results if r.get("pass"))
    by_level: Dict[str, Dict[str, int]] = {}
    for r in results:
        lv = str(r.get("level"))
        bl = by_level.setdefault(lv, {"total": 0, "pass": 0})
        bl["total"] += 1
        if r.get("pass"):
            bl["pass"] += 1
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": len(results),
        "passed": passed,
        "rate": round(passed / len(results), 3) if results else 0,
        "by_level": by_level,
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"\n报告: {REPORT_PATH}")
    print(f"完成率: {passed}/{len(results)}")
    print(json.dumps(by_level, ensure_ascii=False, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
