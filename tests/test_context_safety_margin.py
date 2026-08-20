# -*- coding: utf-8 -*-
"""上下文压缩 token 估算安全边际（排查方案 2.4 深化）。

覆盖：
- estimate_tokens 对 CJK 按 1 字/token 估算（修复中文被低估一半的问题）；
- ContextManager.token_safety_margin 在估算略低于阈值、乘系数后超阈值时触发压缩；
- margin=1.0（默认/直连构造）保持原行为不变；
- 决策日志带安全边际标注；配置默认 1.15 且被 loop 接线。
"""
from agent.config import AppConfig, ContextConfig
from agent.context.manager import ContextManager
from agent.core.decision_logger import DecisionLogger
from agent.prompt.builder import estimate_tokens


def _history(ascii_chars: int):
    """单条 ASCII 内容：估算 ≈ len//2（无空格 → 词数=1）。"""
    return [{"role": "assistant", "content": "a" * ascii_chars}]


def test_estimate_tokens_cjk_counted_per_char():
    # 旧算法 len//2=2，新算法 CJK 按 1 字/token
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("分析需求并规划步骤。") == 10
    # 混合：CJK 逐字 + 其余按原估算
    assert estimate_tokens("hi 你好") == 2 + max(1, 3 // 2)


def test_estimate_tokens_ascii_unchanged():
    assert estimate_tokens("hello world") == max(2, 11 // 2)
    assert estimate_tokens("") == 0


def test_safety_margin_triggers_compression_at_boundary():
    # max_tokens=1000, threshold=0.8 → 阈值 800
    cm_default = ContextManager(max_tokens=1000, compression_threshold=0.8)
    cm_margin = ContextManager(max_tokens=1000, compression_threshold=0.8,
                               token_safety_margin=1.15)
    history = _history(1400)  # 原始估算 700：margin=1.0 不触发，1.15 后 805 触发
    assert not cm_default.should_compact(history)
    assert cm_margin.should_compact(history)


def test_margin_clamped_to_at_least_1_0():
    cm = ContextManager(max_tokens=1000, compression_threshold=0.8,
                        token_safety_margin=0.5)
    assert cm.token_safety_margin == 1.0
    assert not cm.should_compact(_history(1400))


def test_estimate_total_applies_margin():
    cm = ContextManager(max_tokens=1000, compression_threshold=0.8,
                        token_safety_margin=1.15)
    history = _history(1400)
    assert cm._estimate_total(history) == 804 > 800


def test_decision_log_records_safety_margin():
    logger = DecisionLogger()
    cm = ContextManager(max_tokens=1000, compression_threshold=0.8,
                        token_safety_margin=1.15, decision_logger=logger)
    assert cm.should_compact(_history(1400))
    rec = [d for d in logger.records() if d["name"] == "trigger_compression"]
    assert rec and "安全边际x1.15" in rec[0]["decision"]


def test_config_default_and_loop_wiring():
    assert ContextConfig().compression_safety_margin == 1.15
    assert AppConfig().context.compression_safety_margin == 1.15
