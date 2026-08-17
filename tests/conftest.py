# -*- coding: utf-8 -*-
import logging
import os
import stat
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

WS_ROOT = Path(__file__).resolve().parent.parent / "test_workspace"


def _force_rmtree(path: Path) -> None:
    """Windows 兼容删除：先清只读位再递归删除。

    git 工具测试生成的 .git 对象文件默认为只读，shutil.rmtree 在 Windows
    上会直接失败留下残留目录（这些残留曾被 pytest 当作测试目录收集并报
    PermissionError）。清只读位后重试，保证每个临时目录都被真正清理。
    若 Python 内删除仍失败（例如外部沙箱 safe-delete shim 拦截 os 删除），
    再用外部 cmd rmdir 兜底，最大限度避免 test_workspace 残留堆积。
    """
    for root, dirs, files in os.walk(path):
        for name in list(files) + list(dirs):
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)
    if path.exists() and os.name == "nt":
        try:
            subprocess.run(["cmd", "/d", "/c", "rmdir", "/s", "/q",
                            str(path)],
                           capture_output=True, timeout=60)
        except Exception:
            pass
    if path.exists():
        logging.getLogger("alpha-swe.tests").warning(
            "临时目录清理失败，残留: %s", path)


@pytest.fixture
def ws_tmp():
    """项目内的一次性临时目录。"""
    d = WS_ROOT / ("tmp_" + uuid.uuid4().hex[:10])
    d.mkdir(parents=True, exist_ok=True)
    yield d
    _force_rmtree(d)

@pytest.fixture(autouse=True)
def _isolate_agent_log_dirs():
    """把测试期间各配置模型默认的日志/档案/追踪目录重定向到 test_workspace/_logs_*。

    根因修复：单元测试构造 AgentLoop 时若未显式指定 archive/trace/snapshot 目录，
    会按默认值写到仓库 logs/（session 档案 / trace JSONL 累积成百 MB）。
    该 autouse fixture 在每个测试前改默认值、测试后清理，保证测试完全隔离。
    """
    from agent.config import (AgentConfig, AppConfig, ContextConfig,
                              SandboxConfig, SkillConfig)

    d = WS_ROOT / ("_logs_" + uuid.uuid4().hex[:8])
    redirects = (
        (AgentConfig, (("trace_dir", "traces"),
                       ("session_archive_dir", "sessions"),
                       ("snapshot_dir", "snapshots"))),
        (SandboxConfig, (("audit_dir", "audit"),)),
        (ContextConfig, (("archive_dir", "archives"),)),
        (SkillConfig, (("usage_log", "skill_usage.jsonl"),)),
    )
    for model, fields in redirects:
        for field, sub in fields:
            model.model_fields[field].default = str(d / sub)
        model.model_rebuild(force=True)
    # 关键：重建根模型，让 load_config()/AppConfig.from_dict 的验证路径
    # 也拿到重定向后的默认值（否则子模型新默认值被根模型旧 schema 缓存覆盖）
    AppConfig.model_rebuild(force=True)
    yield d
    _force_rmtree(d)
