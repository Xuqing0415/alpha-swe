# -*- coding: utf-8 -*-
"""SWE-bench 评估：应用 patch、执行测试、判定 resolved。

两种模式：
1. 本地回退（默认）：在干净的 git 快照上依次应用 Agent patch 与
   test_patch，然后对 FAIL_TO_PASS / PASS_TO_PASS 逐条运行 pytest。
2. 官方 swebench（可选）：安装 ``swebench`` 后调用其官方评估逻辑，
   与本仓库保持结果口径一致。
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("swe_eval.evaluate")


def try_import_swebench() -> Any:
    """尝试导入官方 swebench；不可用时返回 None。"""
    try:
        import swebench  # noqa: F401
        return swebench
    except ImportError:
        return None


# ---------------- patch 应用 ----------------
def apply_patch(repo_dir: Path, patch_text: str,
                strip: int = 1) -> Tuple[bool, str]:
    """把统一 diff 应用到仓库；优先 git apply，失败回退 patch -pN。"""
    if not patch_text.strip():
        return True, "empty patch"
    if _run(["git", "apply", "--whitespace=nowarn", "-"],
            repo_dir, patch_text):
        return True, "applied via git apply"
    patch_bin = shutil.which("patch")
    if patch_bin:
        if _run([patch_bin, "-p", str(strip), "-N", "--forward", "-"],
                repo_dir, patch_text):
            return True, "applied via patch"
    return False, "patch 应用失败（git apply 与 patch 均失败）"


def _run(cmd: List[str], cwd: Path, input_text: Optional[str] = None) -> bool:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), input=input_text, capture_output=True,
            text=True, timeout=300, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0:
            return True
        logger.debug("命令失败 %s: %s", " ".join(cmd), proc.stderr.strip())
    except Exception as e:
        logger.debug("命令异常 %s: %s", " ".join(cmd), e)
    return False


# ---------------- 测试执行 ----------------
def run_test_node(eval_ws: Path, node: str, timeout: float = 300.0,
                  python: Optional[str] = None) -> Dict[str, Any]:
    """运行单个 pytest 节点（文件或 file::test）。

    返回 {node, status, returncode, output}；status ∈ passed/failed/error/
    timeout/skipped。
    """
    py = python or sys.executable
    cmd = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", node]
    try:
        proc = subprocess.run(
            cmd, cwd=str(eval_ws), capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"node": node, "status": "timeout",
                "returncode": -1, "output": f"pytest 超时（>{timeout:g}s）"}
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        status = "passed"
    elif " no tests ran" in out or "collected 0 items" in out:
        status = "skipped"
    else:
        status = "failed"
    return {"node": node, "status": status,
            "returncode": int(proc.returncode), "output": out[-4000:]}


def evaluate_patch(repo_dir: Path, instance, patch_text: str,
                   eval_ws: Path | None = None,
                   timeout: float = 300.0,
                   python: Optional[str] = None,
                   install_cmd: Optional[str] = None,
                   base_commit: Optional[str] = None) -> Dict[str, Any]:
    """在干净快照上评估 Agent patch。

    返回：
    {
      "resolved": bool, "fail_to_pass": [...], "pass_to_pass": [...],
      "tests_total/passed/failed/skipped": int,
      "install_ok": bool, "apply_ok": bool, "apply_note": str,
      "error": str
    }
    """
    result: Dict[str, Any] = {
        "resolved": False, "fail_to_pass": [], "pass_to_pass": [],
        "tests_total": 0, "tests_passed": 0, "tests_failed": 0,
        "tests_skipped": 0, "install_ok": True, "apply_ok": False,
        "apply_note": "", "error": "",
    }
    eval_ws = eval_ws or (Path(repo_dir).parent /
                          (Path(repo_dir).name + "__eval"))
    try:
        base = base_commit or instance.base_commit
        eval_ws = make_eval_worktree(repo_dir, base, eval_ws)
    except Exception as e:
        result["error"] = f"创建评估快照失败: {e}"
        return result

    # 1) 应用 Agent patch
    ok, note = apply_patch(eval_ws, patch_text)
    result["apply_ok"] = ok
    result["apply_note"] = note
    if not ok:
        result["error"] = note
        return result

    # 2) 应用测试补丁
    if instance.test_patch.strip():
        ok, note = apply_patch(eval_ws, instance.test_patch)
        if not ok:
            result["error"] = f"test_patch 应用失败: {note}"
            return result

    # 3) 可选依赖安装
    if install_cmd:
        proc = subprocess.run(
            install_cmd, shell=True, cwd=str(eval_ws), capture_output=True,
            text=True, timeout=900, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result["install_ok"] = proc.returncode == 0
        if not result["install_ok"]:
            result["error"] = f"依赖安装失败: {proc.stderr.strip()[-2000:]}"
            return result

    # 4) 逐条运行测试
    fail_results = [run_test_node(eval_ws, n, timeout, python)
                    for n in instance.fail_to_pass]
    pass_results = [run_test_node(eval_ws, n, timeout, python)
                    for n in instance.pass_to_pass]
    result["fail_to_pass"] = fail_results
    result["pass_to_pass"] = pass_results
    fail_ok = all(r["status"] == "passed" for r in fail_results)
    pass_ok = all(r["status"] == "passed" for r in pass_results)
    counts = {"passed": 0, "failed": 0, "skipped": 0, "timeout": 0}
    for r in fail_results + pass_results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    result.update({
        "resolved": bool(fail_ok and pass_ok and instance.fail_to_pass),
        "tests_total": len(fail_results) + len(pass_results),
        "tests_passed": counts["passed"],
        "tests_failed": counts["failed"] + counts["timeout"],
        "tests_skipped": counts["skipped"],
    })
    return result


# ---------------- 官方 swebench 桥接 ----------------
def official_eval_config(instance) -> Optional[Dict[str, Any]]:
    """尝试用官方 swebench 生成 eval 配置；不可用返回 None。"""
    mod = try_import_swebench()
    if mod is None:
        return None
    try:
        from swebench.harness.test_spec import make_test_spec
        spec = make_test_spec(instance.to_dict())
        return {"test_spec": spec, "instance": instance.to_dict()}
    except Exception as e:
        logger.warning("swebench 官方配置生成失败，使用本地回退: %s", e)
        return None
def make_eval_worktree(repo_dir: Path, base_commit: str,
                       eval_ws: Path) -> Path:
    """从 Agent 工作仓库创建干净的评估快照（检出 base_commit）。

    用 ``git archive`` 输出 tar 流 + Python tarfile 解包，再把快照初始化为
    独立 git 仓库并提交，保证 ``git apply`` 能正确创建新文件（git apply
    在非 git 目录下会静默 no-op）。避免 git worktree 在 Windows/沙箱环境
    下的路径与 sh 子进程问题。
    """
    eval_ws = Path(eval_ws)
    if eval_ws.exists():
        shutil.rmtree(eval_ws, ignore_errors=True)
    eval_ws.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "archive", "--format=tar", base_commit],
        capture_output=True, timeout=600,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "git archive 失败: "
            + proc.stderr.decode("utf-8", "replace")[:300])
    eval_ws.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tf:
        try:
            tf.extractall(eval_ws, filter="tar")
        except (TypeError, ValueError):
            tf.extractall(eval_ws)  # Python <3.12 无 filter 参数
    _init_snapshot_repo(eval_ws)
    return eval_ws


def _init_snapshot_repo(eval_ws: Path) -> None:
    """把快照目录初始化为独立 git 仓库（含基线提交）。"""
    _run(["git", "init", "-q", "-b", "main"], eval_ws)
    _run(["git", "config", "user.email", "swe-eval@local"], eval_ws)
    _run(["git", "config", "user.name", "swe-eval"], eval_ws)
    _run(["git", "add", "-A"], eval_ws)
    if not _run(["git", "commit", "-q", "-m", "base snapshot"], eval_ws):
        # 空仓库（无文件）时提交失败，可接受
        logger.debug("eval 快照无内容，跳过基线提交: %s", eval_ws)
