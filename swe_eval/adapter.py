# -*- coding: utf-8 -*-
"""SweAgentAdapter —— 把 Alpha-SWE Agent 包装为 SWE-bench 可调用的统一接口。

输入：issue 文本 + 仓库根目录
输出：统一 diff 格式的 patch 文件

实现方式：以子进程调用 ``python -m agent run ... --output json``，任务结束后
用 ``git diff``（含新增文件 intent-to-add）提取最终修改。

测试注入点：``runner`` 参数可替换真实子进程执行（返回
``subprocess.CompletedProcess`` 兼容对象）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("swe_eval.adapter")

_GIT_DIFF_ARGS = ["diff", "--no-ext-diff", "--no-color", "--",
                  ".", ":(exclude).swe-agent"]


@dataclass
class AdapterResult:
    """单次 Agent 求解的结果。"""

    instance_id: str
    ok: bool
    status: str
    exit_code: int
    tokens: int = 0
    elapsed_s: float = 0.0
    rounds: int = 0
    llm_calls: int = 0
    files_modified: List[str] = field(default_factory=list)
    patch: str = ""
    error: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "ok": self.ok,
            "status": self.status,
            "exit_code": self.exit_code,
            "tokens": self.tokens,
            "elapsed_s": self.elapsed_s,
            "rounds": self.rounds,
            "llm_calls": self.llm_calls,
            "files_modified": self.files_modified,
            "patch": self.patch,
            "error": self.error,
        }


def extract_json_payload(text: str) -> Optional[Dict[str, Any]]:
    """从子进程 stdout 中稳健提取第一个 JSON 对象（容错日志/警告前缀）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _git(*args: str, cwd: Path, timeout: float = 120) -> str:
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=timeout,
        cwd=str(cwd), encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败 [{proc.returncode}]: "
            f"{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def extract_patch(repo_dir: Path) -> str:
    """提取 Agent 相对 HEAD 的全部改动（含新增文件），排除 .swe-agent。"""
    repo_dir = Path(repo_dir).resolve()
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "add", "-N", "--",
         ".", ":(exclude).swe-agent"],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        logger.warning("git add -N 失败（忽略）: %s", proc.stderr.strip())
    return _git(*_GIT_DIFF_ARGS, cwd=repo_dir)


class SweAgentAdapter:
    """统一适配器：issue + 仓库 -> patch 文件。"""

    def __init__(
        self,
        config_path: Path | str,
        python: Optional[str] = None,
        timeout: float = 1800.0,
        max_cost: Optional[float] = None,
        max_tokens: Optional[int] = None,
        docker: bool = False,
        runner: Optional[Callable[[List[str]], Any]] = None,
        env: Optional[Dict[str, str]] = None,
    ):
        self.config_path = Path(config_path)
        self.python = python or sys.executable
        self.timeout = float(timeout)
        self.max_cost = max_cost
        self.max_tokens = max_tokens
        self.docker = docker
        self.runner = runner  # 测试注入点
        self.env = dict(os.environ)
        if env:
            self.env.update(env)

    def build_command(self, prompt: str, repo_dir: Path) -> List[str]:
        cmd = [self.python, "-X", "utf8", "-m", "agent", "run", prompt,
               "--config", str(self.config_path),
               "--workspace", str(Path(repo_dir).resolve()),
               "--output", "json",
               "--timeout", str(self.timeout)]
        if self.max_cost and self.max_cost > 0:
            cmd += ["--max-cost", str(self.max_cost)]
        if self.max_tokens:
            cmd += ["--max-tokens", str(self.max_tokens)]
        if not self.docker:
            cmd += ["--disable-docker"]
        return cmd

    def _run(self, cmd: List[str], timeout: float) -> Any:
        if self.runner is not None:
            return self.runner(cmd)
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def solve(self, prompt: str, repo_dir: Path,
              patch_path: Optional[Path] = None) -> AdapterResult:
        """对单个 issue 求解并返回结果；patch 写入 patch_path（可选）。"""
        started = time.time()
        result = AdapterResult(instance_id="", ok=False, status="failed",
                               exit_code=1)
        try:
            proc = self._run(self.build_command(prompt, repo_dir),
                             self.timeout + 60)
        except subprocess.TimeoutExpired:
            result.status = "timeout"
            result.error = f"Agent 执行超时（>{self.timeout:g}s）"
            result.elapsed_s = round(time.time() - started, 2)
            return result
        except Exception as e:  # 启动/执行异常
            result.error = f"Agent 执行异常: {e}"
            result.elapsed_s = round(time.time() - started, 2)
            return result

        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""
        payload = extract_json_payload(stdout)
        result.payload = payload or {}
        result.exit_code = int(getattr(proc, "returncode", 1) or 1)
        result.status = str(result.payload.get("status") or
                            ("completed" if result.exit_code == 0 else "failed"))
        result.ok = bool(result.payload.get("ok")
                         if result.payload else result.exit_code == 0)
        result.tokens = int(result.payload.get("tokens") or 0)
        result.rounds = int(result.payload.get("rounds") or 0)
        result.llm_calls = int(result.payload.get("llm_calls") or 0)
        result.elapsed_s = float(result.payload.get("elapsed_s") or
                                 round(time.time() - started, 2))
        result.files_modified = list(result.payload.get("files_modified") or [])
        if result.exit_code != 0:
            result.error = str(result.payload.get("error")
                               or result.payload.get("final_answer")
                               or stderr.strip() or "任务失败")

        # 提取 patch（即使 Agent 报告失败也可能有部分改动）
        try:
            result.patch = extract_patch(repo_dir)
        except Exception as e:
            logger.warning("提取 patch 失败: %s", e)
            result.patch = ""
        if patch_path is not None:
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text(result.patch, encoding="utf-8")
        return result

    def solve_instance(self, instance, repo_dir: Path,
                       patch_path: Optional[Path] = None) -> AdapterResult:
        result = self.solve(instance.prompt, repo_dir, patch_path)
        result.instance_id = instance.instance_id
        return result
