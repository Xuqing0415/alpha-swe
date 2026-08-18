# -*- coding: utf-8 -*-
"""基准项目本地 pytest 配置：tmp_path 重定向到项目内可写目录（沙箱兼容）。"""
import os
import shutil
import stat
import uuid
from pathlib import Path

import pytest

_TMP_BASE = Path(__file__).resolve().parent / ".pytest_tmp"


@pytest.fixture
def tmp_path():
    d = _TMP_BASE / uuid.uuid4().hex[:10]
    d.mkdir(parents=True, exist_ok=True)
    yield d
    for root, dirs, files in os.walk(d):
        for name in list(files) + list(dirs):
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(d, ignore_errors=True)
