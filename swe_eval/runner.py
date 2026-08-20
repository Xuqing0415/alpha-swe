# -*- coding: utf-8 -*-
"""SWE-bench 批量运行器：准备仓库 -> 求解 -> 评估 -> 结果归档。

资源控制：max_parallel 限制并发子进程数（建议不超过 CPU 核数）；
每个实例独立目录，结果实时写入 results.jsonl（崩溃不丢数据）。
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from swe_eval.adapter import SweAgentAdapter
from swe_eval.dataset import Instance, prepare_repo
from swe_eval.evaluate import evaluate_patch

logger = logging.getLogger("swe_eval.runner")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _instance_dir(results_dir: Path, instance_id: str) -> Path:
    d = results_dir / instance_id
    d.mkdir(parents=True, exist_ok=True)
    return d


class SweBenchRunner:
    """按实例批量执行与归档。"""

    def __init__(
        self,
        adapter: SweAgentAdapter,
        results_dir: Path | str,
        max_parallel: int = 2,
        evaluate: bool = True,
        eval_timeout: float = 300.0,
        python: Optional[str] = None,
        install_cmd: Optional[str] = None,
        keep_repos: bool = False,
        on_result: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.adapter = adapter
        self.results_dir = Path(results_dir)
        self.max_parallel = max(1, int(max_parallel))
        self.evaluate = evaluate
        self.eval_timeout = eval_timeout
        self.python = python
        self.install_cmd = install_cmd
        self.keep_repos = keep_repos
        self.on_result = on_result
        self._repos_dir = self.results_dir / "_repos"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = self.results_dir / "results.jsonl"
        self._lock_file.touch(exist_ok=True)

    def _append_result(self, result: Dict[str, Any]) -> None:
        with self._lock_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")
        if self.on_result:
            self.on_result(result)

    def run_one(self, instance: Instance) -> Dict[str, Any]:
        """单个实例：准备仓库 -> 求解 -> 评估 -> 归档。"""
        started = time.time()
        inst_dir = _instance_dir(self.results_dir, instance.instance_id)
        try:
            repo_dir = prepare_repo(instance, self._repos_dir)
        except Exception as e:
            result = {
                "instance_id": instance.instance_id,
                "repo": instance.repo,
                "base_commit": instance.base_commit,
                "status": "error",
                "error": f"仓库准备失败: {e}",
                "elapsed_s": round(time.time() - started, 2),
                "adapter": {}, "eval": {},
            }
            self._write_instance_files(inst_dir, instance, result, None, {})
            self._append_result(result)
            return result

        patch_path = inst_dir / "patch.diff"
        adapter_result = self.adapter.solve_instance(instance, repo_dir,
                                                     patch_path)
        adapter_result.instance_id = instance.instance_id

        eval_result: Dict[str, Any] = {}
        if self.evaluate and adapter_result.patch.strip():
            eval_ws = inst_dir / "eval_ws"
            eval_result = evaluate_patch(
                repo_dir, instance, adapter_result.patch,
                eval_ws=eval_ws, timeout=self.eval_timeout,
                python=self.python, install_cmd=self.install_cmd,
            )
        elif self.evaluate and not adapter_result.patch.strip():
            eval_result = {"resolved": False, "error": "Agent 未产生任何改动"}

        if adapter_result.status in ("timeout", "budget", "interrupted"):
            status = adapter_result.status
        elif adapter_result.error and not adapter_result.patch.strip():
            status = "error"
        elif eval_result.get("resolved"):
            status = "resolved"
        elif eval_result:
            status = "unresolved"
        else:
            status = "completed_no_eval"

        result = {
            "instance_id": instance.instance_id,
            "repo": instance.repo,
            "base_commit": instance.base_commit,
            "status": status,
            "error": adapter_result.error,
            "elapsed_s": round(time.time() - started, 2),
            "adapter": adapter_result.to_dict(),
            "eval": eval_result,
        }
        self._write_instance_files(inst_dir, instance, result,
                                   adapter_result, eval_result)
        self._append_result(result)
        # 方向一 2.1：保留会话轨迹（logs/sessions/）供深度失败归因
        session_src = repo_dir / "logs" / "sessions"
        if session_src.is_dir():
            try:
                shutil.copytree(session_src, inst_dir / "session",
                                dirs_exist_ok=True)
            except OSError as e:
                logger.warning("会话轨迹保存失败: %s", e)
        if not self.keep_repos:
            # 每个实例只保留结果文件，避免克隆仓库堆积磁盘
            shutil.rmtree(repo_dir, ignore_errors=True)
        logger.info("[%s] status=%s elapsed=%.1fs",
                    instance.instance_id, status, result["elapsed_s"])
        return result

    @staticmethod
    def _write_instance_files(inst_dir: Path, instance: Instance,
                              result: Dict[str, Any], adapter_result,
                              eval_result: Dict[str, Any]) -> None:
        (inst_dir / "problem_statement.txt").write_text(
            instance.prompt, encoding="utf-8")
        (inst_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if adapter_result is not None:
            (inst_dir / "adapter.json").write_text(
                json.dumps(adapter_result.to_dict(), ensure_ascii=False,
                           indent=2), encoding="utf-8")
        if eval_result:
            (inst_dir / "eval.json").write_text(
                json.dumps(eval_result, ensure_ascii=False, indent=2),
                encoding="utf-8")

    def run_many(self, instances: List[Instance],
                 max_instances: Optional[int] = None) -> List[Dict[str, Any]]:
        """并发执行多个实例；返回结果列表（与输入顺序一致）。"""
        if max_instances:
            instances = instances[:max_instances]
        results: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=self.max_parallel,
                                thread_name_prefix="swe_eval") as pool:
            futures = {pool.submit(self.run_one, inst): inst
                       for inst in instances}
            for fut in as_completed(futures):
                inst = futures[fut]
                try:
                    results[inst.instance_id] = fut.result()
                except Exception as e:
                    logger.exception("实例执行异常: %s", inst.instance_id)
                    results[inst.instance_id] = {
                        "instance_id": inst.instance_id,
                        "repo": inst.repo,
                        "base_commit": inst.base_commit,
                        "status": "error",
                        "error": f"runner 异常: {e}",
                        "elapsed_s": 0.0, "adapter": {}, "eval": {},
                    }
        if not self.keep_repos:
            # 批量结束清理本地镜像，避免仓库对象堆积磁盘
            shutil.rmtree(self._repos_dir / "_mirror", ignore_errors=True)
        return [results[i.instance_id] for i in instances
                if i.instance_id in results]


# ---------------- 指标聚合 ----------------
def aggregate_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总一批结果，计算解决率与辅助指标。"""
    total = len(results)
    if total == 0:
        return {"total": 0}
    status_counts: Dict[str, int] = {}
    resolved = 0
    tokens = elapsed = rounds = 0.0
    for r in results:
        status_counts[r.get("status", "unknown")] = \
            status_counts.get(r.get("status", "unknown"), 0) + 1
        if r.get("status") == "resolved":
            resolved += 1
        tokens += float(r.get("adapter", {}).get("tokens", 0) or 0)
        rounds += float(r.get("adapter", {}).get("rounds", 0) or 0)
        elapsed += float(r.get("elapsed_s", 0) or 0)
    eval_ok = sum(1 for r in results if r.get("eval", {}).get("resolved"))
    return {
        "total": total,
        "resolved": resolved,
        "resolve_rate": round(resolved / total, 4) if total else 0.0,
        "eval_ok": eval_ok,
        "status_counts": status_counts,
        "avg_tokens": round(tokens / total, 1) if total else 0,
        "avg_rounds": round(rounds / total, 2) if total else 0,
        "avg_elapsed_s": round(elapsed / total, 1) if total else 0,
        "total_elapsed_s": round(elapsed, 1),
    }


def save_report(results_dir: Path | str, instances: List[Instance],
                results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总并落盘 summary.json（含实例元数据）。"""
    results_dir = Path(results_dir)
    summary = aggregate_results(results)
    meta = [{"instance_id": i.instance_id, "repo": i.repo,
             "base_commit": i.base_commit} for i in instances]
    report = {"generated_at": _now(), "summary": summary,
              "instances": meta}
    (results_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
