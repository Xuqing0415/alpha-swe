"""测试公共夹具。

沙箱只允许写项目工作区，因此临时目录统一放在 ./test_workspace 下
（该目录已被 .gitignore 忽略）。
"""
import shutil
import uuid
from pathlib import Path

import pytest

WS_ROOT = Path(__file__).resolve().parent.parent / "test_workspace"


@pytest.fixture
def ws_tmp():
    """项目内的一次性临时目录。"""
    d = WS_ROOT / ("tmp_" + uuid.uuid4().hex[:10])
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)