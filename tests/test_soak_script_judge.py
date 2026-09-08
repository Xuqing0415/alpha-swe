# -*- coding: utf-8 -*-
"""长时间浸泡打卡脚本（scripts/run_soak_long.py）的判定逻辑单测。

覆盖：
- 短跑预热探测（运行不足 WARMUP_SECONDS / 采样不足）只报 warmup，不误报泄漏；
- 足够时长与采样后，RSS/句柄斜率超阈值才判 fail；
- 单会话事件超界是硬性失败，warmup 阶段同样不豁免。
"""
import importlib.util
from pathlib import Path

_SCRIPT = (Path(__file__).resolve().parents[1]
           / "scripts" / "run_soak_long.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "run_soak_long_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _load()


def test_warmup_short_run_not_reported_as_leak():
    # 预热期即使 RSS 斜率超阈值也只报 warmup，避免 10 分钟内短探针误报
    verdict, ok = _MOD._judge(False, True, True,
                              {"ok": False}, {"ok": True})
    assert verdict == "warmup"
    assert ok is True


def test_warmup_insufficient_samples_not_reported_as_leak():
    verdict, ok = _MOD._judge(True, False, True,
                              {"ok": False}, {"ok": True})
    assert verdict == "warmup"
    assert ok is True


def test_leak_after_warmup_is_fail():
    verdict, ok = _MOD._judge(True, True, True,
                              {"ok": False}, {"ok": True})
    assert verdict == "fail"
    assert ok is False


def test_healthy_after_warmup_is_ok():
    verdict, ok = _MOD._judge(True, True, True,
                              {"ok": True}, {"ok": True})
    assert verdict == "ok"
    assert ok is True


def test_event_overflow_fails_even_during_warmup():
    # 单会话事件超界是硬性边界，warmup 也不豁免
    verdict, ok = _MOD._judge(False, True, False,
                              {"ok": True}, {"ok": True})
    assert verdict == "warmup"
    assert ok is False


def test_constants_set_for_long_runs():
    assert _MOD.WARMUP_SECONDS >= 600
    assert _MOD.MIN_FIT_SAMPLES >= 5
    assert _MOD.REGRESSION_WINDOW >= _MOD.MIN_FIT_SAMPLES
