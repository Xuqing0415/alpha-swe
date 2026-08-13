"""LLM 交互层 —— 统一接口，默认 mock（测试/离线），可切换到 LiteLLM。

对应设计第 15 节「LLM 交互 | LiteLLM / aiohttp」。
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent.config import LLMConfig, LLMProvider

logger = logging.getLogger("alpha-swe.llm")

Message = Dict[str, str]  # {"role": "system|user|assistant", "content": "..."}


class LLMServiceError(RuntimeError):
    """LLM 调用在重试耗尽后仍失败（超时 / 空响应 / 服务端瞬时错误）。"""


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
    """通过 litellm 统一调用多种模型（openai/anthropic/ollama/...）。

    收敛期 P0 加固（阶段一 1.1「LLM 调用」注入点）：
    - 单次调用 asyncio.wait_for 超时兜底（config.llm.timeout）；
    - 超时 / 空响应 / 服务端瞬时错误按指数退避重试 config.llm.max_retries 次；
    - 重试耗尽后抛 LLMServiceError，由 AgentLoop 统一降级为任务 FAILED，
      不会让会话崩溃或无限卡死。
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        try:
            import litellm  # noqa: F401
        except ImportError as e:
            raise RuntimeError("使用 litellm provider 需要安装 litellm") from e
        self._litellm = __import__("litellm")
        self.timeout = float(getattr(config, "timeout", 120.0) or 120.0)
        self.max_retries = int(getattr(config, "max_retries", 2) or 2)

    async def _acomplete(self, kwargs: Dict[str, Any]) -> Any:
        """可注入的底层调用点（测试用 stub 替换，避免真实网络）。"""
        return await self._litellm.acompletion(**kwargs)

    @staticmethod
    def _extract_content(resp: Any) -> str:
        try:
            content = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        return "" if content is None else str(content).strip()

    async def complete(self, messages: List[Message]) -> str:
        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        api_base = self.config.api_base or self.config.base_url
        if api_base:
            kwargs["api_base"] = api_base
        if self.config.api_key_env:
            kwargs["api_key"] = __import__("os").environ.get(self.config.api_key_env, "")

        last_error: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await asyncio.wait_for(
                    self._acomplete(kwargs), timeout=self.timeout)
                content = self._extract_content(resp)
                if not content:
                    last_error = LLMServiceError("LLM 返回空响应")
                else:
                    return content
            except asyncio.TimeoutError:
                last_error = TimeoutError(
                    f"LLM 调用超时（>{self.timeout:.0f}s）")
            except Exception as e:  # 网络/服务端瞬时错误 -> 重试
                last_error = e
            if attempt < self.max_retries:
                await asyncio.sleep(2 ** attempt)  # 1s / 2s 指数退避
        raise LLMServiceError(
            f"LLM 调用在 {self.max_retries + 1} 次尝试后仍失败: {last_error}"
        ) from last_error


def build_llm(config: LLMConfig) -> BaseLLM:
    """按配置构造 LLM 客户端。"""
    if config.provider in (
        LLMProvider.LITELLM, LLMProvider.OPENAI,
        LLMProvider.ANTHROPIC, LLMProvider.OLLAMA,
    ):
        # 统一走 LiteLLM 路由（openai/anthropic/ollama 通过 model 前缀区分）
        return LiteLLMClient(config)
    return MockLLM()