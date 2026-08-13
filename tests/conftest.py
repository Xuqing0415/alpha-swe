# -*- coding: utf-8 -*-
import os
import stat
import shutil
import uuid
from pathlib import Path

import pytest

WS_ROOT = Path(__file__).resolve().parent.parent / "test_workspace"


def _force_rmtree(path: Path) -> None:
    """Windows 兼容删除：先清只读位再递归删除。

    git 工具测试生成的 .git 对象文件默认为只读，shutil.rmtree 在 Windows
    上会直接失败留下残留目录（这些残留曾被 pytest 当作测试目录收集并报
    PermissionError）。清只读位后重试，保证每个临时目录都被真正清理。
    """
    for root, dirs, files in os.walk(path):
        for name in list(files) + list(dirs):
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def ws_tmp():
    """项目内的一次性临时目录。"""
    d = WS_ROOT / ("tmp_" + uuid.uuid4().hex[:10])
    d.mkdir(parents=True, exist_ok=True)
    yield d
    _force_rmtree(d)