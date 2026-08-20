"""Alpha-SWE Agent 命令行入口（``python -m agent``）。

收敛期 P1「CLI 标准化」：让 Agent 可被脚本 / CI 稳定调用。

用法::

    python -m agent run "任务描述" [--config PATH] [--workspace PATH]
        [--output json|text] [--timeout SECONDS] [--max-cost DOLLARS]
        [--cost-per-1k-tokens RATE] [--max-tokens N]
        [--disable-docker] [--enable-mcp]

任务描述可从 stdin 读取（省略位置参数，或传 ``-``）::

    echo "修复登录空指针" | python -m agent run --output json

退出码:

    0  任务成功完成
    1  任务执行失败
    2  用户中断 / 用法错误
    3  超时（--timeout 到期）
    4  预算超限（--max-cost 到期）

说明:

    - 非交互式 CLI 默认不连接 MCP 服务器（避免外部依赖阻塞任务），
      需要时用 ``--enable-mcp`` 显式开启；
    - Docker 沙箱沿用配置文件（config/agent.yaml），可用 ``--disable-docker`` 关闭；
    - 未指定 ``--workspace`` 时默认以当前目录（cwd）为工作区；
    - stdin 中文任务描述在 Windows PowerShell 5.1 管道下可能被替换为
      ``?``（US-ASCII 编码），检测到时会给出警告；建议直接传位置参数。
    - ``--max-tokens`` 覆盖上下文 token 上限（压缩阈值按配置比例计算）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.attribution import classify_failure
from agent.config import AppConfig, load_config
from agent.core.loop import AgentLoop, LoopResult
from agent.errorlog import print_error, write_error_log
from agent.observability.archive import (
    files_modified_from_events as extract_files_modified,
)
from agent.selfcheck import critical_failed, format_selfcheck, run_selfcheck

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INTERRUPT = 2
EXIT_TIMEOUT = 3
EXIT_BUDGET = 4

# 预算熔断轮询周期（秒）与默认每千 token 估算成本（美元）
BUDGET_POLL_INTERVAL = 0.5
DEFAULT_COST_PER_1K = 0.002

_STATUS_NAMES = {
    EXIT_OK: "completed",
    EXIT_FAILED: "failed",
    EXIT_INTERRUPT: "interrupted",
    EXIT_TIMEOUT: "timeout",
    EXIT_BUDGET: "budget",
}


class UsageError(Exception):
    """CLI 用法错误（缺任务描述等）。"""


def _version() -> str:
    try:
        from agent import __version__

        return __version__
    except Exception:
        return "0.1.0"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """解析命令行参数（`python -m agent run ...`）。"""
    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description="Alpha-SWE Agent：可脚本化调用的 SWE 任务执行入口。",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    run_p = sub.add_parser(
        "run",
        help="执行一个任务并返回结构化结果",
        description=(
            "执行一个 SWE 任务（支持真实 LLM 与离线 mock）。"
            "机器可读场景请配合 --output json 使用。"
        ),
    )
    run_p.add_argument("prompt", nargs="?", default=None,
                       help="任务描述；省略或传 `-` 时从 stdin 读取")
    run_p.add_argument("--config", default=None,
                       help="配置文件路径（默认 config/agent.yaml）")
    run_p.add_argument("--workspace", default=None,
                       help="工作区目录（覆盖 sandbox.workspace）")
    run_p.add_argument("--output", choices=["text", "json"], default="text",
                       help="输出格式（默认 text；脚本/CI 用 json）")
    run_p.add_argument("--timeout", type=float, default=None,
                       help="任务硬性时间上限（秒），超时退出码 3")
    run_p.add_argument("--max-cost", type=float, default=None,
                       help="token 预算上限（美元），超限退出码 4")
    run_p.add_argument("--cost-per-1k-tokens", type=float,
                       default=DEFAULT_COST_PER_1K,
                       help="每千 token 估算成本（默认 %s 美元）"
                            % DEFAULT_COST_PER_1K)
    run_p.add_argument("--max-tokens", type=int, default=None,
                       help="上下文 token 上限（覆盖 agent.max_token_limit）")
    run_p.add_argument("--disable-docker", action="store_true",
                       help="关闭 Docker 沙箱，改用本地工具层")
    run_p.add_argument("--enable-mcp", action="store_true",
                       help="开启 MCP 服务器连接（CLI 默认关闭）")
    run_p.add_argument("--self-check", action="store_true",
                       help="仅运行启动自检并退出（0=关键检查全部通过）")
    run_p.add_argument("--version", action="version",
                       version="alpha-swe " + _version())
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> AppConfig:
    """加载配置并应用 CLI 覆盖项（workspace / docker / mcp / token 上限）。"""
    cfg = load_config(args.config)
    if args.workspace:
        cfg.sandbox.workspace = str(
            Path(args.workspace).expanduser().resolve())
    else:
        # CLI 默认以当前目录为工作区：cd 进项目后直接操作项目文件，
        # 而不是 ./workspace 沙箱子目录（真实项目场景的预期行为）。
        cfg.sandbox.workspace = str(Path.cwd().resolve())
    if args.disable_docker:
        cfg.sandbox.docker_enabled = False
    if args.enable_mcp:
        # 显式开启 MCP 服务器连接（CLI 默认关闭，避免外部依赖阻塞任务）
        cfg.mcp.enabled = True
    else:
        cfg.mcp.enabled = False
    if args.max_tokens:
        # 压缩阈值按 context.max_tokens * compression_threshold 计算
        cfg.context.max_tokens = int(args.max_tokens)
        cfg.agent.max_token_limit = int(args.max_tokens)
    return cfg


def read_prompt(args: argparse.Namespace) -> str:
    """任务描述来源：位置参数 > stdin（省略参数或传 `-`）。"""
    if args.prompt and args.prompt != "-":
        return args.prompt
    data = sys.stdin.read()
    prompt = data.strip().lstrip("\ufeff")
    # Windows PowerShell 5.1 管道默认 $OutputEncoding=US-ASCII，
    # 中文会被替换成 '?'（并常带 BOM）；检测疑似乱码并给出可操作提示，
    # 避免把损坏的任务描述喂给模型（信息已丢失，无法恢复，只能提示）。
    if "\ufeff" in data or prompt.count("?") >= 3:
        print(
            "[警告] 任务描述疑似存在管道编码问题（中文被替换为 ?）。"
            "建议改用位置参数：python -m agent run \"任务描述\"；"
            "或先执行：$OutputEncoding = [Console]::OutputEncoding = "
            "[Text.Encoding]::UTF8",
            file=sys.stderr,
        )
    if not prompt:
        raise UsageError("未提供任务描述：请传位置参数，或通过 stdin 输入")
    return prompt


def estimate_cost(counters: Dict[str, Any], rate_per_1k: float) -> float:
    """按 metrics 的 token_usage 计数估算成本（美元）。"""
    tokens = float(counters.get("token_usage", 0.0) or 0.0)
    return round(tokens / 1000.0 * rate_per_1k, 6)


def make_payload(result: Optional[LoopResult], loop: AgentLoop,
                 exit_code: int, elapsed_s: float,
                 rate_per_1k: float,
                 error: Optional[str] = None) -> Dict[str, Any]:
    """组装稳定的机器可读输出 schema（不随内部实现变化）。"""
    snap = loop.metrics.snapshot()
    counters = snap.get("counters", {})
    gauges = snap.get("gauges", {})
    tokens = int(counters.get("token_usage", 0.0) or 0.0)
    payload: Dict[str, Any] = {
        "ok": exit_code == EXIT_OK,
        "status": _STATUS_NAMES.get(exit_code, "failed"),
        "final_answer": (result.final_answer if result else "")
                        or (error or ""),
        "rounds": (result.total_rounds if result
                   else int(gauges.get("rounds", 0) or 0)),
        "tasks": {
            "total": int(gauges.get("tasks_total", 0) or 0),
            "completed": int(gauges.get("tasks_completed", 0) or 0),
            "failed": int(gauges.get("tasks_failed", 0) or 0),
            "skipped": int(gauges.get("tasks_skipped", 0) or 0),
        },
        "llm_calls": int(counters.get("llm_calls", 0.0) or 0.0),
        "tokens": tokens,
        "elapsed_s": round(elapsed_s, 2),
        "cost_est": estimate_cost(counters, rate_per_1k),
        "files_modified": extract_files_modified(loop.events),
        "exit_code": exit_code,
    }
    if error:
        payload["error"] = error
    if exit_code != EXIT_OK:
        # 收敛期 P2：失败任务附带归因类别与改进建议（供复盘 / CI 分析）
        try:
            payload["attribution"] = classify_failure(
                events=loop.events,
                decisions=loop._decision.records(),
                metrics=snap,
                final_answer=payload["final_answer"],
            )
        except Exception as e:  # 归因计算失败不破坏正常输出
            payload["attribution"] = {
                "category": "unknown",
                "label": "未知",
                "reason": "归因计算失败: %s" % e,
                "suggestions": [],
            }
    return payload


def _default_loop_factory(cfg: AppConfig) -> AgentLoop:
    return AgentLoop(config=cfg)


async def _drive(loop: AgentLoop, prompt: str, timeout: Optional[float],
                 max_cost: Optional[float],
                 rate_per_1k: float
                 ) -> Tuple[int, Optional[LoopResult], Optional[str]]:
    """驱动 loop.run，处理超时与预算熔断；返回 (退出码, 结果, 错误信息)。

    预算熔断：后台监控任务轮询 metrics 的 token_usage，超过 --max-cost
    即取消运行任务（与超时同级的硬中断），保证长任务不会无限消耗 token。
    """
    run_task = asyncio.create_task(loop.run(prompt))
    budget_exceeded = asyncio.Event()
    monitor: Optional[asyncio.Task] = None
    if max_cost and max_cost > 0:
        async def _monitor() -> None:
            while True:
                await asyncio.sleep(BUDGET_POLL_INTERVAL)
                snap = loop.metrics.snapshot()
                if estimate_cost(snap.get("counters", {}),
                                 rate_per_1k) >= max_cost:
                    budget_exceeded.set()
                    return

        monitor = asyncio.create_task(_monitor())

    pending = [run_task]
    if monitor is not None:
        pending.append(monitor)
    try:
        done, _pending = await asyncio.wait(
            pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if budget_exceeded.is_set():
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            used = estimate_cost(loop.metrics.snapshot().get("counters", {}),
                                 rate_per_1k)
            return EXIT_BUDGET, None, (
                f"预算超限（已消耗 ${used:.4f} >= 上限 ${max_cost:.4f}）")
        if run_task not in done:
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return EXIT_TIMEOUT, None, (
                f"任务超时（>{timeout:g}s，已强制终止）")
        result = run_task.result()
        if result.ok:
            return EXIT_OK, result, None
        return EXIT_FAILED, result, (result.final_answer or "任务失败")
    except asyncio.CancelledError:
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return EXIT_INTERRUPT, None, "任务被用户取消"
    except Exception as e:  # run() 内部未捕获的异常
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return EXIT_FAILED, None, f"任务执行异常: {e}"
    finally:
        if monitor is not None and not monitor.done():
            monitor.cancel()


async def _run(loop: AgentLoop, prompt: str, timeout: Optional[float],
               max_cost: Optional[float],
               rate_per_1k: float) -> Tuple[int, Optional[LoopResult],
                                           Optional[str]]:
    try:
        return await _drive(loop, prompt, timeout, max_cost, rate_per_1k)
    finally:
        await loop.close()


def _emit(payload: Dict[str, Any], output_format: str) -> None:
    """按 --output 输出结果；错误信息始终附加到 stderr。"""
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        lines = [
            "状态: %s" % payload["status"],
            "轮次: %s" % payload["rounds"],
            "token: %s（估算成本 $%.4f）"
            % (payload["tokens"], payload["cost_est"]),
            "LLM 调用: %s" % payload["llm_calls"],
            "耗时: %ss" % payload["elapsed_s"],
        ]
        tasks = payload["tasks"]
        if tasks.get("total"):
            lines.append(
                "任务: %s 个（完成 %s / 失败 %s / 跳过 %s）"
                % (tasks["total"], tasks["completed"],
                   tasks["failed"], tasks["skipped"]))
        if payload.get("files_modified"):
            lines.append("修改文件:")
            lines.extend("  - %s" % p for p in payload["files_modified"])
        answer = payload.get("final_answer") or payload.get("error") or ""
        if answer:
            lines.append("最终答复:")
            lines.append(answer)
        print("\n".join(lines))
    if payload.get("error"):
        print("[%s] %s" % (payload["status"], payload["error"]),
              file=sys.stderr)


def _cli_context(args: argparse.Namespace) -> Dict[str, Any]:
    """错误日志上下文：入口参数摘要（不含敏感值）。"""
    return {
        "command": getattr(args, "command", "?"),
        "config": str(getattr(args, "config", "") or ""),
        "workspace": str(getattr(args, "workspace", "") or ""),
        "output": str(getattr(args, "output", "text")),
        "prompt": str(getattr(args, "prompt", "") or "")[:120],
    }


def _report_fatal(exc: BaseException, args: argparse.Namespace,
                  phase: str = "cli") -> None:
    """统一错误出口（方案 1.1）：全量 traceback + 上下文落盘并打印。"""
    ctx = {**_cli_context(args), "phase": phase}
    path = write_error_log(exc, context=ctx)
    print_error(exc, context=ctx, log_path=path)


def run_cli(args: argparse.Namespace,
            loop_factory: Optional[Callable[[AppConfig], AgentLoop]] = None
            ) -> int:
    """解析后执行 CLI；返回进程退出码。"""
    factory = loop_factory or _default_loop_factory
    try:
        cfg = build_config(args)
    except Exception as e:
        _report_fatal(e, args, phase="config")
        return EXIT_FAILED
    # 启动自检（方案 1.3）：任务开始前暴露配置/环境问题
    if getattr(args, "self_check", False):
        items = run_selfcheck(cfg)
        sys.stderr.write(format_selfcheck(items) + "\n")
        return EXIT_OK if not critical_failed(items) else EXIT_FAILED
    try:
        items = run_selfcheck(cfg)
        sys.stderr.write(format_selfcheck(items) + "\n")
        failed = critical_failed(items)
        if failed:
            sys.stderr.write(
                "[警告] %d 项关键自检未通过，任务将继续但能力可能受限\n"
                % len(failed))
    except Exception as e:
        sys.stderr.write("[警告] 启动自检异常: %s\n" % e)
    try:
        prompt = read_prompt(args)
    except UsageError as e:
        print("用法错误: %s" % e, file=sys.stderr)
        return EXIT_INTERRUPT
    try:
        loop = factory(cfg)
    except Exception as e:
        _report_fatal(e, args, phase="init")
        return EXIT_FAILED
    started = time.time()
    try:
        exit_code, result, error = asyncio.run(
            _run(loop, prompt, args.timeout, args.max_cost,
                 args.cost_per_1k_tokens))
    except KeyboardInterrupt:
        raise
    except Exception as e:
        _report_fatal(e, args, phase="run")
        return EXIT_FAILED
    elapsed = time.time() - started
    payload = make_payload(result, loop, exit_code, elapsed,
                           args.cost_per_1k_tokens, error)
    _emit(payload, args.output)
    return exit_code


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口：python -m agent run ..."""
    args = parse_args(argv)
    if not getattr(args, "command", None):
        # 未指定子命令：打印提示并正常退出
        print("用法: python -m agent run \"任务描述\" [选项]（详情见 --help）")
        return EXIT_OK
    try:
        return run_cli(args)
    except KeyboardInterrupt:
        print("用户中断", file=sys.stderr)
        return EXIT_INTERRUPT
    except UsageError as e:
        print("用法错误: %s" % e, file=sys.stderr)
        return EXIT_INTERRUPT
    except Exception as e:
        _report_fatal(e, args, phase="cli")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
