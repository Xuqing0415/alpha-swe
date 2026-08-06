# -*- coding: utf-8 -*-
"""阶段四：上下文压缩质量测试 —— 工具输出压缩、分级压缩、保真度、决策日志增强。"""
from agent.context.manager import ContextManager
from agent.core.decision_logger import DecisionLogger


def _long_output():
    """超过 output_truncate 的长工具输出：含错误行 + 大段噪音。"""
    head = "\n".join([
        "Traceback (most recent call last):",
        '  File "src/app.py", line 42, in run',
        "ValueError: invalid literal for int()",
        "INFO: building package ...",
    ])
    return head + "\n" + ("noise line\n" * 400)  # 总长 > 2000


def _history(total_chars: int, n: int = 6, obs_chars: int = 500):
    """构造 history：observation 位于前部（会被压缩），总字符约 total_chars。"""
    per = max(10, total_chars // n)
    msgs = [
        {"role": "assistant", "content": "A" * per},
        {"role": "observation", "content": "O" * obs_chars},
        {"role": "tool_call", "content": "terminal_execute"},
        {"role": "assistant", "content": "C" * per},
        {"role": "assistant", "content": "D" * per},
        {"role": "assistant", "content": "E" * per},
    ]
    return msgs[:n]


def _decisions(logger):
    return {d["name"]: d for d in logger.records()}


# ---- 工具输出压缩：关键行保留 + 原始存档 ----

def test_long_output_keeps_error_lines_and_archives(tmp_path):
    logger = DecisionLogger()
    cm = ContextManager(max_tokens=100_000, compression_threshold=0.1,
                        archive_dir=str(tmp_path / "archives"),
                        output_truncate=2000,
                        decision_logger=logger)
    history = [
        {"role": "assistant", "content": "先看报错"},
        {"role": "tool_call", "content": "terminal_execute"},
        {"role": "observation", "content": _long_output()},
        {"role": "assistant", "content": "B" * 100},
        {"role": "assistant", "content": "C" * 100},
        {"role": "assistant", "content": "D" * 100},
    ]
    marker = cm.compact(history)
    # 关键错误行保留在摘要里
    assert "ValueError" in marker
    assert "Traceback" in marker
    # 摘要引用原始输出存档路径
    assert "原始输出" in marker and "ctx_archive" in marker
    archives = list((tmp_path / "archives").glob("ctx_archive_*.txt"))
    assert len(archives) == 1
    raw = archives[0].read_text(encoding="utf-8")
    assert "Traceback" in raw and "noise line" in raw  # 原始完整存档
    # 决策日志：压缩级别 + 丢弃消息 ID
    dec = _decisions(logger)
    assert dec["compression_level"]["decision"].startswith("压缩级别: light")
    assert "old#2(long-output)" in dec["compressed_message_ids"]["decision"]


# ---- 分级压缩 ----

def test_compression_levels_by_pressure(tmp_path):
    # light：压力 < medium_threshold，只压缩长输出
    logger = DecisionLogger()
    cm = ContextManager(max_tokens=100_000, compression_threshold=0.01,
                        archive_dir=str(tmp_path / "a"),
                        decision_logger=logger)
    history = _history(8000)
    cm.compact(history)
    assert "light" in _decisions(logger)["compression_level"]["decision"]

    # medium：保留决策点（assistant/think/tool_call），observation 截断
    logger2 = DecisionLogger()
    cm2 = ContextManager(max_tokens=10_000, compression_threshold=0.01,
                         archive_dir=str(tmp_path / "b"),
                         decision_logger=logger2)
    history2 = _history(29_000, obs_chars=500)  # tokens ~9900 -> 压力 ~0.99
    marker2 = cm2.compact(history2)
    assert "medium" in _decisions(logger2)["compression_level"]["decision"]
    assert "assistant:" in marker2  # 决策点保留
    assert "tool_call: terminal_execute" in marker2
    obs_part = [p for p in marker2.split("；") if p.startswith("observation:")]
    assert obs_part and len(obs_part[0]) < 200  # observation 被截断（原始 500 字符）

    # heavy：全部递归摘要
    logger3 = DecisionLogger()
    cm3 = ContextManager(max_tokens=10_000, compression_threshold=0.01,
                         archive_dir=str(tmp_path / "c"),
                         decision_logger=logger3)
    history3 = _history(40_000, obs_chars=500)  # tokens ~13600 -> 压力 1.36
    marker3 = cm3.compact(history3)
    assert "heavy" in _decisions(logger3)["compression_level"]["decision"]
    # heavy 下所有消息都截断为短摘要（内容 <= 120 字符 + 角色前缀）
    parts3 = marker3.split("；")
    assert all(len(p) < 160 for p in parts3)  # 首段含 [Previous summary: 前缀


# ---- 保真度：决策点（关键约束）跨压缩保留 ----

def test_medium_compression_keeps_decision_points(tmp_path):
    logger = DecisionLogger()
    cm = ContextManager(max_tokens=10_000, compression_threshold=0.01,
                        archive_dir=str(tmp_path / "arch"),
                        decision_logger=logger)
    history = [
        {"role": "assistant",
         "content": "必须使用 Python 3.9 语法，不要用 match 语句"},
        {"role": "think", "content": "先检查当前 python 版本"},
        {"role": "tool_call", "content": "terminal_execute: python --version"},
        {"role": "observation", "content": "Python 3.11.9" * 30},
        {"role": "assistant", "content": "A" * 9600},
        {"role": "assistant", "content": "B" * 9600},
    ]
    marker = cm.compact(history)
    assert "Python 3.9" in marker  # 早期关键约束保留
    assert "python --version" in marker  # 工具决策保留
    dec = _decisions(logger)
    assert "压缩级别: medium" in dec["compression_level"]["decision"]
    assert "->" in dec["compression_level"]["decision"]  # 压缩前后 token


# ---- 决策日志增强 ----

def test_compression_decision_log_details(tmp_path):
    logger = DecisionLogger()
    cm = ContextManager(max_tokens=10_000, compression_threshold=0.5,
                        archive_dir=str(tmp_path / "arch"),
                        output_truncate=2000,
                        decision_logger=logger)
    history = [
        {"role": "assistant", "content": "A" * 3000},
        {"role": "tool_call", "content": "terminal_execute"},
        {"role": "observation", "content": _long_output()},
        {"role": "assistant", "content": "B" * 3000},
        {"role": "assistant", "content": "C" * 3000},
        {"role": "assistant", "content": "D" * 3000},
    ]
    assert cm.should_compact(history)  # 触发压缩决策
    cm.compact(history)
    dec = _decisions(logger)
    assert "trigger_compression" in dec
    assert "compression_level" in dec
    assert "compressed_message_ids" in dec
    assert "compression_method" in dec