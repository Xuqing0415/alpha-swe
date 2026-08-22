# -*- coding: utf-8 -*-
"""SWE-bench 数据集加载与仓库准备（方向二·阶段一）。

支持两种输入：
1. 本地 JSONL（SWE-bench 官方导出格式，逐行一个实例）；
2. HuggingFace datasets（princeton-nlp/SWE-bench_Lite 等），datasets 库可选安装。

实例字段（与官方 SWE-bench 对齐）：
- instance_id / repo / base_commit
- problem_statement（issue 文本，Agent 输入）
- patch / test_patch（gold patch 与测试补丁，评估参考）
- FAIL_TO_PASS / PASS_TO_PASS（测试用例清单）
- hints_text / version / created_at（可选）
"""
from __future__ import annotations

import json
import logging
import os
import random
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("swe_eval.dataset")

_CORE_FIELDS = {
    "instance_id", "repo", "base_commit", "problem_statement",
    "patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS",
    "hints_text", "version", "created_at",
}


@dataclass
class Instance:
    """单个 SWE-bench 任务。"""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    patch: str = ""
    test_patch: str = ""
    fail_to_pass: List[str] = field(default_factory=list)
    pass_to_pass: List[str] = field(default_factory=list)
    hints_text: str = ""
    version: str = ""
    created_at: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Instance":
        return cls(
            instance_id=str(d.get("instance_id") or ""),
            repo=str(d.get("repo") or ""),
            base_commit=str(d.get("base_commit") or ""),
            problem_statement=str(d.get("problem_statement") or ""),
            patch=str(d.get("patch") or ""),
            test_patch=str(d.get("test_patch") or ""),
            fail_to_pass=list(d.get("FAIL_TO_PASS") or []),
            pass_to_pass=list(d.get("PASS_TO_PASS") or []),
            hints_text=str(d.get("hints_text") or ""),
            version=str(d.get("version") or ""),
            created_at=str(d.get("created_at") or ""),
            extra={k: v for k, v in d.items() if k not in _CORE_FIELDS},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "problem_statement": self.problem_statement,
            "patch": self.patch,
            "test_patch": self.test_patch,
            "FAIL_TO_PASS": self.fail_to_pass,
            "PASS_TO_PASS": self.pass_to_pass,
            "hints_text": self.hints_text,
            "version": self.version,
            "created_at": self.created_at,
            **self.extra,
        }

    @property
    def prompt(self) -> str:
        """发给 Agent 的 issue 文本（含 hints，若有）。"""
        text = self.problem_statement or ""
        if self.hints_text:
            text = f"{text}\n\n参考提示（hints）:\n{self.hints_text}"
        return text.strip()

    @property
    def all_test_nodes(self) -> List[str]:
        """FAIL_TO_PASS + PASS_TO_PASS 合并去重后的测试节点清单。"""
        seen: List[str] = []
        for node in list(self.fail_to_pass) + list(self.pass_to_pass):
            if node not in seen:
                seen.append(node)
        return seen


_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                   "http_proxy", "https_proxy", "all_proxy")
_proxy_bypass: Optional[bool] = None  # None=未探测；True=代理不可达需绕过
_proxy_probe_lock = threading.Lock()


def _proxy_unreachable() -> bool:
    """探测环境代理是否可达；不可达时 git 应绕过直连。"""
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        val = os.environ.get(key, "").strip()
        if not val or val.lower() == "none":
            continue
        try:
            parts = urlsplit(val if "://" in val else "http://" + val)
            host = parts.hostname or "127.0.0.1"
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            continue
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return False  # 代理可达，保留
        except OSError:
            logger.warning(
                "环境代理不可达 %s://%s:%s，git 子进程将绕过代理直连",
                parts.scheme or "http", host, port)
            return True
    return False


def _git_env() -> Dict[str, str]:
    """git 子进程环境：代理不可达时剔除代理变量（结果缓存，仅探测一次）。"""
    global _proxy_bypass
    env = dict(os.environ)
    if not any(k in env for k in _PROXY_ENV_KEYS):
        return env
    with _proxy_probe_lock:
        if _proxy_bypass is None:
            _proxy_bypass = _proxy_unreachable()
        bypass = _proxy_bypass
    if bypass:
        for k in _PROXY_ENV_KEYS:
            env.pop(k, None)
    return env


def _run_git(args: List[str], cwd: Optional[Path] = None,
             timeout: float = 600) -> subprocess.CompletedProcess:
    """执行 git，Windows 下隐藏控制台窗口；失败时抛 RuntimeError。"""
    cmd = ["git", *args]
    kwargs: Dict[str, Any] = {
        "capture_output": True, "text": True, "timeout": timeout,
        "encoding": "utf-8", "errors": "replace",
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "env": _git_env(),
    }
    if cwd is not None:
        # 仅显式 cwd 时传入；否则 str(None)="None" 会被当作目录，
        # 在 Windows 上抛 WinError 267（克隆等无 cwd 调用秒败）
        kwargs["cwd"] = str(cwd)
    try:
        proc = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git 命令超时: {' '.join(cmd)}") from None
    except FileNotFoundError:
        raise RuntimeError("未找到 git 可执行文件，请先安装 git") from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"git 命令失败 [{proc.returncode}]: {' '.join(cmd)}\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}")
    return proc


# ---------------- 加载 ----------------
def load_instances_jsonl(path: Path | str) -> List[Instance]:
    """从本地 JSONL 文件加载实例。"""
    path = Path(path)
    instances: List[Instance] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path}:{line_no} JSON 解析失败: {e}") from e
            instances.append(Instance.from_dict(data))
    return instances


def load_instances_file(path: Path | str) -> List[Instance]:
    """按扩展名自动选择加载方式（.jsonl 逐行 / .json 数组）。"""
    path = Path(path)
    if path.suffix == ".jsonl":
        return load_instances_jsonl(path)
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return [Instance.from_dict(d) for d in data]
    if isinstance(data, dict) and "instances" in data:
        return [Instance.from_dict(d) for d in data["instances"]]
    raise ValueError(f"不支持的实例文件格式: {path}")


def load_hf_subset(name: str = "princeton-nlp/SWE-bench_Lite",
                   split: str = "test",
                   max_instances: Optional[int] = None,
                   seed: Optional[int] = None,
                   column_map: Optional[Dict[str, str]] = None) -> List[Instance]:
    """从 HuggingFace datasets 加载实例（需要安装 datasets 库）。"""
    try:
        from datasets import load_dataset
    except ImportError as e:
        raise RuntimeError(
            "加载 HuggingFace 数据集需要安装 datasets：pip install datasets"
        ) from e
    column_map = column_map or {}
    rows = load_dataset(name, split=split)
    instances = []
    for row in rows:
        raw = {k: v for k, v in row.items() if k in _CORE_FIELDS}
        for target, source in column_map.items():
            if target not in raw and source in row:
                raw[target] = row[source]
        # HuggingFace 空列表字段可能是 None
        raw["FAIL_TO_PASS"] = list(raw.get("FAIL_TO_PASS") or [])
        raw["PASS_TO_PASS"] = list(raw.get("PASS_TO_PASS") or [])
        instances.append(Instance.from_dict(raw))
    if seed is not None:
        random.Random(seed).shuffle(instances)
    if max_instances:
        instances = instances[:max_instances]
    return instances


def select_subset(instances: Iterable[Instance], n: int,
                  seed: Optional[int] = None) -> List[Instance]:
    """按固定种子选取可复现子集（默认保持原顺序，n<=0 表示全部）。"""
    lst = list(instances)
    if n and n > 0 and n < len(lst):
        if seed is not None:
            rng = random.Random(seed)
            lst = rng.sample(lst, n)
        else:
            lst = lst[:n]
    return lst


def save_instances_jsonl(instances: Iterable[Instance],
                         path: Path | str) -> Path:
    """把实例列表写回 JSONL，便于固化评估子集。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for inst in instances:
            fh.write(json.dumps(inst.to_dict(), ensure_ascii=False) + "\n")
    return path


# ---------------- 仓库准备 ----------------
def repo_dir_name(instance: Instance) -> str:
    return f"{instance.repo.replace('/', '__')}__{instance.instance_id}"


# 每 repo 一个线程锁：同 repo 多个实例并发时只建一次镜像
_mirror_locks: Dict[str, threading.Lock] = {}
_mirror_locks_guard = threading.Lock()


def _mirror_lock(repo: str) -> threading.Lock:
    with _mirror_locks_guard:
        return _mirror_locks.setdefault(repo, threading.Lock())


def prepare_repo(instance: Instance, work_root: Path | str,
                 git_bin: str = "git") -> Path:
    """克隆（或复用）仓库并检出 base_commit，创建评估分支。

    同一 work_root 下按实例复用；同 repo 多个实例共享本地镜像
    （work_root/_mirror/<repo>），实例克隆走 --reference 秒级检出，
    避免每个实例重复全量克隆同一仓库。
    """
    work_root = Path(work_root)
    repo_dir = work_root / repo_dir_name(instance)
    if (repo_dir / ".git").exists():
        return repo_dir
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    if repo_dir.exists():
        logger.warning("仓库目录存在但缺少 .git，删除后重新克隆: %s", repo_dir)
        import shutil
        shutil.rmtree(repo_dir, ignore_errors=True)
    if not instance.repo or not instance.base_commit:
        raise ValueError(f"实例缺少 repo/base_commit: {instance.instance_id}")
    url = f"https://github.com/{instance.repo}.git"
    mirror = work_root / "_mirror" / instance.repo.replace("/", "__")
    with _mirror_lock(instance.repo):
        if not (mirror / "HEAD").exists():
            logger.info("建立本地镜像 %s -> %s"
                        "（首次全量克隆，预计 1-10 分钟，请耐心等待）",
                        url, mirror)
            _run_git(["clone", "--mirror", "--quiet", url, str(mirror)],
                     timeout=1800)
        else:
            logger.info("复用本地镜像 %s", mirror)
        logger.info("从镜像克隆 %s -> %s", url, repo_dir)
        _run_git(["clone", "--quiet", "--no-tags", "--reference",
                  str(mirror), url, str(repo_dir)], timeout=1800)
    _run_git(["checkout", "--quiet", instance.base_commit], cwd=repo_dir)
    _run_git(["checkout", "-b", "alphaswe_eval"], cwd=repo_dir)
    return repo_dir
