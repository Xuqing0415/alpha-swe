# -*- coding: utf-8 -*-
"""SSE 事件序列化与流式生成。

序列化统一交给 :mod:`agent.observability.event_schema`：``to_sse`` 保证
输出格式（``event: <type>`` + ``data: <json>`` + 空行）与历史实现一致，
``validate_event`` / ``describe`` 只在严格模式下参与自检，不改变发送行为。
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from agent.observability.event_schema import describe, to_sse, validate_event

logger = logging.getLogger("alpha-swe.server.events")

# 控制帧：不属于业务事件，不参与 schema 校验（避免未知类型噪音）。
_CONTROL_EVENTS = ("ping", "done")


async def sse_generator(queue: asyncio.Queue,
                        strict_validation: bool = False) -> AsyncIterator[str]:
    """把 asyncio.Queue 中的事件流式输出为 SSE 文本。

    事件格式：``event: <type>`` + ``data: <json>``；收到 done 事件后结束。
    事件缺少 ``type`` 时沿用 ``"message"`` 缺省名（与历史行为一致，不让
    ``normalize_event`` 的 ``unknown`` 兜底改写出参）。

    ``strict_validation=True`` 时对每个业务事件调用 ``validate_event``，
    校验不通过（结构性问题或未知类型）只记 ``warning`` 并照常发送；
    默认 False 不做校验、不产生任何日志。
    """
    try:
        while True:
            item = await queue.get()
            event_type = item.get("type", "message")
            if strict_validation and event_type not in _CONTROL_EVENTS:
                problems = validate_event(item)
                if problems:
                    logger.warning("SSE 事件校验不通过: %s | %s",
                                   problems, describe(item))
            yield to_sse(item, event_name=event_type)
            if event_type == "done":
                return
    except asyncio.CancelledError:
        return
