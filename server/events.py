# -*- coding: utf-8 -*-
"""SSE 事件序列化与流式生成。"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict

import asyncio


async def sse_generator(queue: asyncio.Queue) -> AsyncIterator[str]:
    """把 asyncio.Queue 中的事件流式输出为 SSE 文本。

    事件格式：``event: <type>`` + ``data: <json>``；收到 done 事件后结束。
    """
    try:
        while True:
            item = await queue.get()
            event_type = item.get("type", "message")
            data = item.get("data", {})
            if event_type == "ping":
                yield ": ping\n\n"
                continue
            payload = json.dumps(data, ensure_ascii=False, default=str)
            yield f"event: {event_type}\ndata: {payload}\n\n"
            if event_type == "done":
                return
    except asyncio.CancelledError:
        return
