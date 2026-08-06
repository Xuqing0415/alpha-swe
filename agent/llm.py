"""LLM 交互层 —— 统一接口，默认 mock（测试/离线），可切换到 LiteLLM。

对应设计第 15 节「LLM 交互 | LiteLLM / aiohttp」。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent.config import LLMConfig

logger = logging.getLogger("alpha-swe.llm")

Message = Dict[str, str]  # {"role": "system|user|assistant", "content": "..."}


class BaseLLM(ABC):
    @abstractmethod
    async def complete(self, messages: List[Message]) -> str:
        """返回模型文本响应。"""


@dataclass
class MockLLM(BaseLLM):
    """可编程 mock：通过 responder 脚本化响应，供测试与离线演示。"""
    responder: Optional[Callable[[List[Message]], str]] = None
    calls: List[List[Message]] = field(default_factory=list)

    async def complete(self, messages: List[Message]) -> str:
        self.calls.append(messages)
        if self.responder is not None:
            return self.responder(messages)
        return '{"final_answer": "mock 完成"}'


class LiteLLMClient(BaseLLM):
    """通过 litellm 统一调用多种模型（openai/anthropic/ollama/...）。"""

    def __init__(self, config: LLMConfig):
        self.config = config
        try:
            import litellm  # noqa: F401
        except ImportError as e:
            raise RuntimeError("使用 litellm provider 需要安装 litellm") from e
        self._litellm = __import__("litellm")

    async def complete(self, messages: List[Message]) -> str:
        import litellm

        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if self.config.base_url:
            kwargs["api_base"] = self.config.base_url
        if self.config.api_key_env:
            kwargs["api_key"] = __import__("os").environ.get(self.config.api_key_env, "")
        resp = await litellm.acompletion(**kwargs)
        return resp["choices"][0]["message"]["content"]


def build_llm(config: LLMConfig) -> BaseLLM:
    """按配置构造 LLM 客户端。"""
    if config.provider == "litellm":
        return LiteLLMClient(config)
    return MockLLM()