# -*- coding: utf-8 -*-
"""会话状态提炼（主线一 1.3C）——把有价值的临时状态转为项目记忆。

会话结束时先收集「可能具有跨会话价值」的候选：完成结果、项目约定类证据、
用户目标；过滤掉行号/临时目录/轮次等一次性的现场信息。若配置了 LLM，
再让模型在候选中挑选真正可复用、且不含密钥的知识；模型不可用或输出非法时
回退到确定性候选，保证功能可用。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, List, Optional

from agent.redact import redact_secrets

logger = logging.getLogger("alpha-swe.memory.distill")

_EPHEMERAL = re.compile(
    r"(刚才|本轮|这一轮|临时文件|临时目录|临时路径|第\s*\d+\s*行|"
    r"line\s*\d+|tmp[/\\]|temp[/\\]|token\s*[:=]|轮次\s*\d+|"
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})", re.I)
_VALUABLE_KEYS = ("project", "project_knowledge", "convention", "architecture",
                  "config", "contract", "decision")


def _clean(text: Any, limit: int = 500) -> str:
    value = redact_secrets(str(text or "")).strip()
    return value[:limit]


def _is_ephemeral(text: str) -> bool:
    return bool(_EPHEMERAL.search(text or ""))


def collect_candidates(state: Any, result: Any,
                       prompt: str = "") -> List[str]:
    """收集跨会话候选知识（确定性、去重、有界）。"""
    out: List[str] = []

    def add(text: Any, limit: int = 500) -> None:
        value = _clean(text, limit)
        if len(value) < 8 or _is_ephemeral(value):
            return
        if value not in out:
            out.append(value)

    final_answer = getattr(result, "final_answer", "") if result else ""
    add(final_answer, 500)
    for task in list(getattr(result, "tasks", None) or []):
        if str(getattr(task, "status", "")).endswith("completed"):
            instruction = getattr(task, "instruction", "")
            task_result = getattr(task, "result", "")
            if task_result:
                add(f"{instruction}: {task_result}", 400)
            else:
                add(instruction, 240)
    evidence = getattr(state, "evidence", {}) if state is not None else {}
    for key, value in dict(evidence or {}).items():
        if any(marker in str(key).lower() for marker in _VALUABLE_KEYS):
            add(f"{key}: {value}", 400)
    if prompt and len(str(prompt)) <= 200:
        add(f"用户目标: {prompt}", 240)
    return out[:8]


def _parse_items(response: str) -> List[str]:
    text = str(response or "").strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if match:
        text = match.group(1)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        value = _clean(item, 500)
        if len(value) >= 8 and not _is_ephemeral(value) and value not in out:
            out.append(value)
    return out[:5]


async def distill_session(llm: Optional[Any], state: Any, result: Any,
                          prompt: str = "") -> List[str]:
    """提炼会话知识；LLM 可选，失败时回退确定性候选。"""
    candidates = collect_candidates(state, result, prompt)
    if not candidates:
        return []
    if llm is None:
        return candidates[:5]
    messages = [
        {"role": "system", "content": (
            "你负责把一次软件工程会话提炼为跨会话可复用的项目记忆。"
            "只保留项目约定、架构事实、可复用的解决方案或重要决策；"
            "丢弃行号、临时路径、本轮/刚才等一次性状态与任何密钥。"
            "只输出 JSON：{\"items\": [\"...\"]}，最多 5 条，每条不超过 200 字。"
        )},
        {"role": "user", "content": "候选状态：\n" + "\n".join(
            f"- {item}" for item in candidates)},
    ]
    try:
        response = await llm.complete(messages)
        items = _parse_items(response)
        if items:
            return items
    except Exception as exc:  # noqa: BLE001 - 提炼失败不影响任务收尾
        logger.warning("会话知识提炼 LLM 失败，回退确定性候选: %s", exc)
    return candidates[:5]


__all__ = ["collect_candidates", "distill_session"]
