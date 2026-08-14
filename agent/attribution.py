"""失败归因分析 —— 收敛期 P2（阶段二 2.2）。

对每个失败任务定位失败类别（规划/检索/理解/工具/上下文/记忆/测试/未知），
并给出对应改进措施；aggregate_failures() 汇总多份会话档案，识别高频失败
模式，形成改进清单。

输入可以是：
- 实时运行状态：classify_failure(events, decisions, metrics, ...)；
- 会话档案：classify_session_failure(doc)（doc 为 alpha-swe-session-v1）。

规则式分类（离线可测）；接入真实 LLM 时可在规则无法判定时调用 LLM 补充。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

# 失败类别 -> 中文标签
ATTRIBUTION_CATEGORIES: Dict[str, str] = {
    "planning": "规划失败",
    "retrieval": "检索失败",
    "understanding": "理解失败",
    "tool": "工具失败",
    "context": "上下文失败",
    "memory": "记忆失败",
    "testing": "测试失败",
    "unknown": "未知",
}

# 类别 -> 改进措施（阶段二 2.2：针对高频失败模式设计改进）
IMPROVEMENT_ACTIONS: Dict[str, str] = {
    "planning": "增强规划器：任务拆分前注入调用图与项目约定摘要，规划失败时记录归因信号",
    "retrieval": "增强代码搜索：增加符号级检索与结构化结果（grep/ast 定位）",
    "understanding": "增强代码理解：注入 AST 摘要与相关符号，解析失败时给出可操作反馈",
    "tool": "增强工具层：超时管控、输出截断、熔断与错误分类（TRANSIENT/PERMANENT）",
    "context": "优化压缩策略：提高触发阈值、保留决策点、压缩后校验关键信息",
    "memory": "增强记忆检索：任务类型过滤、可信度衰减、检索失败降级可见",
    "testing": "增强测试闭环：失败用例结构化提取并回填 Prompt，改完必测",
    "unknown": "人工分析失败案例，补充归因规则",
}

_SEARCH_TOOLS = ("file_search", "file_search_tool", "search")
_TEST_TOOLS = ("run_tests", "test_runner", "test")


def _mk(category: str, reason: str) -> Dict[str, Any]:
    return {
        "category": category,
        "label": ATTRIBUTION_CATEGORIES.get(category, category),
        "reason": reason,
        "suggestions": [IMPROVEMENT_ACTIONS.get(category, "")],
    }


def _decision_names(decisions: List[Dict[str, Any]]) -> List[str]:
    return [str(d.get("name", "")) for d in decisions or []]


def _premature_compression(decisions: List[Dict[str, Any]],
                           events: List[Dict[str, Any]]) -> bool:
    """首次实际压缩（compression_level）发生在 <=2 个决策点之后视为过早。"""
    comp = [d for d in decisions or []
            if d.get("name") == "compression_level"]
    if not comp:
        return False
    first_ts = min(float(d.get("timestamp", 0.0) or 0.0) for d in comp)
    points = [e for e in events or []
              if e.get("type") in ("think", "tool_call", "task_start")]
    before = sum(1 for e in points if float(e.get("ts", 0.0) or 0.0) < first_ts)
    return before <= 2


def _tool_failure_detail(events: List[Dict[str, Any]]) -> str:
    """汇总失败工具调用：tool 名 + action + 是否超时/熔断。"""
    parts: List[str] = []
    seen: set = set()
    for e in events:
        data = e.get("data") or {}
        tool = str(data.get("tool", "?"))
        params = data.get("params") or {}
        action = str(params.get("action", "")) or "?"
        key = f"{tool}:{action}"
        if key in seen:
            continue
        seen.add(key)
        out = str(data.get("output", ""))
        tag = "超时" if ("超时" in out or "timeout" in out.lower()) else "失败"
        parts.append(f"{tool}({action}) {tag}")
    return "; ".join(parts[:5]) or "未知工具错误"


def _retrieval_evidence(events: List[Dict[str, Any]]) -> str:
    """检索失败证据：搜索工具失败或搜索结果为空。"""
    for e in events:
        data = e.get("data") or {}
        tool = str(data.get("tool", ""))
        params = data.get("params") or {}
        action = str(params.get("action", ""))
        is_search = tool in _SEARCH_TOOLS or (
            tool == "file_ops" and action == "search")
        if not is_search:
            continue
        if not data.get("success"):
            return f"代码搜索工具 {tool}({action}) 执行失败"
        output = str(data.get("output", ""))
        if "未匹配到" in output or "没有找到" in output or "0 处" in output:
            return "代码搜索未匹配到目标（关键词/模式可能不正确）"
    return ""


def _test_failure(events: List[Dict[str, Any]]) -> bool:
    for e in events:
        data = e.get("data") or {}
        tool = str(data.get("tool", ""))
        if tool in _TEST_TOOLS and not data.get("success"):
            return True
        # 终端里跑测试框架失败也视为测试失败
        if tool == "terminal_execute" and not data.get("success"):
            out = str(data.get("output", ""))
            if any(k in out.lower() for k in ("pytest", "failed", "tests failed")):
                return True
    return False


def classify_failure(events: Optional[List[Dict[str, Any]]] = None,
                     decisions: Optional[List[Dict[str, Any]]] = None,
                     metrics: Optional[Dict[str, Any]] = None,
                     final_answer: str = "",
                     prompt: str = "") -> Dict[str, Any]:
    """规则式失败归因：返回 {category, label, reason, suggestions}。

    优先级：记忆 > 上下文（过早压缩）> 工具 > 规划 > 检索 > 测试 > 理解 > 未知。
    """
    events = events or []
    decisions = decisions or []
    metrics = metrics or {}
    counters = metrics.get("counters", {}) or {}
    names = _decision_names(decisions)
    tool_events = [e for e in events
                   if e.get("type") == "tool_call" and e.get("data")]
    tool_fails = [e for e in tool_events if not e["data"].get("success")]
    tool_failures = int(counters.get("tool_failures", 0) or 0)
    retries = int(counters.get("retries", 0) or 0)
    compressions = int(counters.get("compressions", 0) or 0)

    # 1) 记忆失败：记忆检索/写入降级
    if "retrieval_error" in names:
        return _mk(
            "memory",
            "长期记忆检索失败已降级（retrieval_error 决策点），任务缺少历史经验")

    # 2) 上下文失败：任务早期被压缩，关键信息可能丢失
    if compressions >= 1 and _premature_compression(decisions, events):
        return _mk(
            "context",
            f"上下文在任务早期被压缩（共 {compressions} 次），关键约束可能丢失")

    # 3) 工具失败：工具调用失败 / 超时 / 熔断
    if tool_failures > 0 or "timeout.strike" in names:
        detail = _tool_failure_detail(tool_fails)
        strikes = names.count("timeout.strike")
        suffix = f"；{strikes} 次超时熔断" if strikes else ""
        return _mk("tool", f"工具失败 {tool_failures} 次：{detail}{suffix}")

    # 4) 规划失败：LLM 规划回退单任务
    if "planner_fallback" in names:
        return _mk("planning", "LLM 规划失败回退单任务（planner_fallback 决策点）")

    # 5) 检索失败：代码搜索无结果/搜索工具失败
    evidence = _retrieval_evidence(tool_events)
    if evidence:
        return _mk("retrieval", evidence)

    # 6) 测试失败：测试运行工具失败
    if _test_failure(tool_events):
        return _mk("testing", "测试运行工具执行失败（run_tests 或测试命令）")

    # 7) 理解失败：输出解析重试/解析失败
    if retries > 0 or "解析失败" in (final_answer or ""):
        return _mk(
            "understanding",
            f"模型输出解析重试 {retries} 次后失败（生成内容无法被解析）")

    # 8) 未知
    return _mk("unknown", "未识别到明确失败信号，需人工分析会话档案")


def classify_session_failure(doc: Dict[str, Any]) -> Dict[str, Any]:
    """对一份会话档案做失败归因（doc 为 alpha-swe-session-v1）。"""
    result = doc.get("result") or {}
    return classify_failure(
        events=doc.get("events", []),
        decisions=doc.get("decisions", []),
        metrics=doc.get("metrics", {}),
        final_answer=str(result.get("final_answer", "")),
        prompt=str(doc.get("prompt", "")),
    )


def aggregate_failures(docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总多份失败会话档案：识别高频失败模式并给出改进清单。"""
    items: List[Dict[str, Any]] = []
    by_category: Counter = Counter()
    for doc in docs:
        result = doc.get("result") or {}
        if result.get("ok"):
            continue  # 只统计失败会话
        attr = classify_session_failure(doc)
        by_category[attr["category"]] += 1
        items.append({
            "session_id": doc.get("session_id", ""),
            "prompt": str(doc.get("prompt", ""))[:120],
            **attr,
        })
    high_frequency = [
        {
            "category": cat,
            "label": ATTRIBUTION_CATEGORIES.get(cat, cat),
            "count": count,
            "suggestions": [IMPROVEMENT_ACTIONS.get(cat, "")],
        }
        for cat, count in by_category.most_common()
    ]
    return {
        "total": len(items),
        "by_category": dict(by_category),
        "high_frequency": high_frequency,
        "items": items,
    }


__all__ = [
    "ATTRIBUTION_CATEGORIES",
    "IMPROVEMENT_ACTIONS",
    "classify_failure",
    "classify_session_failure",
    "aggregate_failures",
]
