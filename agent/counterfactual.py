"""反事实分析 —— 进阶 1.2（深化解释能力）。

任务失败后，对会话档案做两步分析：
1. 复用 agent.attribution 的规则式归因定位失败类别；
2. 对与失败类别相关的关键决策点（工具失败 / 规划回退 / 压缩 / 检索降级 /
   解析重试等）生成 2-3 条"备选方案"（当时也可以这样做），并指出失败
   转折点（哪个决策/事件导致失败）。

分析结果写入长期记忆（kind="counterfactual"，metadata 带 negative/category/
lesson_key），后续相似任务检索命中时以 [反事实警告] 前缀注入 Prompt，
提醒 Agent 避开上次的错误选择。全部规则式实现（离线可测），真实 LLM
补充留作扩展接口。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from agent.attribution import classify_session_failure

# 失败类别 -> 备选方案模板（进阶 1.2：反事实分析）
CATEGORY_ALTERNATIVES: Dict[str, List[Dict[str, str]]] = {
    "planning": [
        {"choice": "规划前先注入调用图与项目约定摘要",
         "rationale": "拆分前看到依赖与约定，避免遗漏关键子任务"},
        {"choice": "把任务拆成更小、可验证的原子步骤",
         "rationale": "每个子任务带完成标准，失败可定位到具体步骤"},
        {"choice": "规划阶段先做一次只读探查（文件树/README）",
         "rationale": "基于项目实际结构拆分，减少凭假设规划"},
    ],
    "retrieval": [
        {"choice": "改用 grep/ast 符号级定位",
         "rationale": "关键词搜索无结果时按符号名与调用关系定位"},
        {"choice": "更换关键词或扩大搜索范围",
         "rationale": "同一概念在不同命名下表达可能不同"},
        {"choice": "直接读取候选文件并利用 AST 摘要",
         "rationale": "用结构信息定位而非只依赖文本匹配"},
    ],
    "understanding": [
        {"choice": "读取调用方与被调用方后再确认意图",
         "rationale": "单文件上下文不足以推断跨模块行为"},
        {"choice": "动手前先复述对任务的理解",
         "rationale": "显式复述可暴露理解偏差"},
        {"choice": "先运行最小复现验证假设",
         "rationale": "用实验确认行为再修改，避免改错位置"},
    ],
    "tool": [
        {"choice": "换用低开销命令并加超时",
         "rationale": "工具失败常因资源或超时，缩小输入规模"},
        {"choice": "先检查输出尾部与错误行再重试",
         "rationale": "失败信息多在尾部，直接重试浪费轮次"},
        {"choice": "拆分为多个小操作",
         "rationale": "单次大操作更易超时或被截断"},
    ],
    "context": [
        {"choice": "提高压缩阈值或禁用早期压缩",
         "rationale": "任务早期关键约束被压缩导致信息丢失"},
        {"choice": "把关键约束写入独立文件持久保存",
         "rationale": "让关键信息不随上下文压缩消失"},
        {"choice": "压缩前显式保留决策点与工具结果",
         "rationale": "决策日志可在压缩后补充上下文"},
    ],
    "memory": [
        {"choice": "降级为无记忆模式继续执行",
         "rationale": "记忆检索失败不应阻断任务主流程"},
        {"choice": "重试记忆后端连接并记录降级",
         "rationale": "短暂故障可通过重试恢复"},
    ],
    "testing": [
        {"choice": "为改动生成测试并先跑最小用例",
         "rationale": "测试失败通常源于改动未验证或用例遗漏"},
        {"choice": "从失败输出提取用例名与断言差异",
         "rationale": "结构化失败信息可精确定位断言差异"},
        {"choice": "先跑受影响文件的相关测试",
         "rationale": "缩小范围加快反馈循环"},
    ],
    "interrupt": [
        {"choice": "在中断点保存快照并从断点恢复",
         "rationale": "用户中断不意味着任务失败，应保留进度"},
    ],
    "unknown": [
        {"choice": "收集完整事件与决策日志供人工分析",
         "rationale": "未知归因需要更多上下文而非自动重试"},
    ],
}

_DECISION_TURNING_POINTS = {
    "planning": ("planner_fallback", "规划回退"),
    "context": ("compression_level", "上下文压缩"),
    "memory": ("retrieval_error", "记忆检索降级"),
    "interrupt": ("interrupt", "用户中断"),
    "understanding": ("parse_retry", "输出解析重试"),
}

_SEARCH_TOOL_NAMES = ("file_search", "file_search_tool", "search")
_TEST_TOOL_NAMES = ("run_tests", "test_runner", "test")


def _turning_point(doc: Dict[str, Any], category: str) -> str:
    """从决策点/事件中定位该失败类别的"转折点"。"""
    events = doc.get("events", []) or []
    decisions = doc.get("decisions", []) or []
    tool_events = [
        e for e in events
        if e.get("type") == "tool_call" and e.get("data")
    ]
    fails = [e for e in tool_events if not e["data"].get("success")]
    if category == "tool" and fails:
        last = fails[-1]["data"]
        return (f"工具调用失败 {last.get('tool')}："
                f"{str(last.get('error') or '')[:100]}")
    if category == "testing":
        for e in fails:
            if e["data"].get("tool") in _TEST_TOOL_NAMES:
                return (f"测试工具失败 {e['data'].get('tool')}："
                        f"{str(e['data'].get('error') or '')[:100]}")
    if category == "retrieval":
        for e in fails:
            tool = e["data"].get("tool", "")
            action = str((e["data"].get("params") or {}).get("action", ""))
            if tool in _SEARCH_TOOL_NAMES or (
                    tool == "file_ops" and action == "search"):
                return (f"搜索工具失败 {tool}："
                        f"{str(e['data'].get('error') or '')[:100]}")
    key, label = _DECISION_TURNING_POINTS.get(category, ("", ""))
    if key:
        for d in reversed(decisions):
            if d.get("name") == key:
                return f"{label}决策: {str(d.get('decision', ''))[:120]}"
    for d in reversed(decisions):
        if d.get("name") in ("tool.reasoning", "final.reasoning"):
            return f"关键决策: {str(d.get('decision', ''))[:120]}"
    if fails:
        return f"最后的失败工具调用: {fails[-1]['data'].get('tool', '')}"
    if events:
        last = events[-1]
        return f"最后事件: {last.get('type')} {str(last.get('data', {}))[:80]}"
    return "（无可定位的转折点）"


def analyze_failure(doc: Dict[str, Any],
                    max_alternatives: int = 3) -> Dict[str, Any]:
    """对失败会话档案做反事实分析。

    返回 {category, label, reason, turning_point, alternatives, lesson}。
    alternatives 为 2-3 条"当时也可以这样做"的备选方案。
    """
    attr = classify_session_failure(doc)
    category = str(attr.get("category", "unknown"))
    label = str(attr.get("label", category))
    alternatives = [
        dict(a) for a in CATEGORY_ALTERNATIVES.get(
            category, CATEGORY_ALTERNATIVES["unknown"])[:max_alternatives]
    ]
    turning = _turning_point(doc, category)
    first_choice = alternatives[0]["choice"] if alternatives else "人工分析"
    lesson = (
        f"该类失败（{label}）的教训：{attr.get('reason', '')}。"
        f"下次遇到相似场景建议：{first_choice}"
    )
    return {
        "category": category,
        "label": label,
        "reason": str(attr.get("reason", "")),
        "turning_point": turning,
        "alternatives": alternatives,
        "lesson": lesson,
    }


def format_lesson_text(analysis: Dict[str, Any],
                       prompt: str = "") -> str:
    """把反事实分析格式化为长期记忆条目文本。"""
    parts = []
    if prompt:
        parts.append(f"失败任务: {str(prompt)[:200]}")
    parts.append(
        f"归因: {analysis.get('label', analysis.get('category', ''))}")
    if analysis.get("turning_point"):
        parts.append(f"转折点: {analysis['turning_point']}")
    alternatives = analysis.get("alternatives", [])[:3]
    if alternatives:
        parts.append("备选方案:")
        parts.extend(
            f"  - 备选{i + 1}: {a.get('choice', '')}"
            f"（{a.get('rationale', '')}）"
            for i, a in enumerate(alternatives)
        )
    if analysis.get("lesson"):
        parts.append(f"教训: {analysis['lesson']}")
    return "\n".join(parts)


def store_lesson(memory, analysis: Dict[str, Any],
                 prompt: str = "") -> bool:
    """写入反事实教训到长期记忆（按 lesson_key 去重）。

    返回是否真正写入；已存在（去重命中）返回 False。记忆禁用时返回 False。
    """
    if getattr(memory, "disabled", False):
        return False
    category = str(analysis.get("category", "unknown"))
    lesson_key = hashlib.sha1(
        f"{category}:{prompt}".encode("utf-8")).hexdigest()[:12]
    try:
        existing = memory.search(
            prompt or str(analysis.get("lesson", "")),
            top_k=5, kinds=["counterfactual"])
        if any((h.get("metadata") or {}).get("lesson_key") == lesson_key
               for h in existing):
            return False
    except Exception:
        pass
    memory.remember(
        "counterfactual",
        format_lesson_text(analysis, prompt),
        {
            "negative": True,
            "counterfactual": True,
            "category": category,
            "lesson_key": lesson_key,
        },
    )
    return True


def build_warning(lessons: List[Dict[str, Any]]) -> str:
    """把命中的反事实教训格式化为 Prompt 警告块。"""
    lines = []
    for h in lessons:
        meta = h.get("metadata") or {}
        category = meta.get("category", "")
        text = str(h.get("text", ""))
        lines.append(f"[反事实警告·{category}] {text}")
    return "\n".join(lines)


def prepend_warnings(hits: List[Dict[str, Any]],
                     memory_text: str = "") -> tuple:
    """从检索结果中分离反事实教训并拼到记忆文本前。

    返回 (合并后的文本, 反事实教训条数)。memory_text 可为空。
    """
    lessons = [
        h for h in (hits or [])
        if h.get("kind") == "counterfactual"
        or (h.get("metadata") or {}).get("counterfactual")
    ]
    if not lessons:
        return memory_text, 0
    block = build_warning(lessons)
    if memory_text:
        return f"{block}\n\n{memory_text}", len(lessons)
    return block, len(lessons)


__all__ = [
    "CATEGORY_ALTERNATIVES",
    "analyze_failure",
    "build_warning",
    "format_lesson_text",
    "prepend_warnings",
    "store_lesson",
]
