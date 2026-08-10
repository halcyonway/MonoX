"""LLMProxy 接口。Loop 不感知具体模型，只看到 LlmChunk 流。"""
from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable

from .events import LlmChunk


@runtime_checkable
class LLMProxy(Protocol):
    """闭环 stream 接口，对齐 OpenAI-compatible stream delta。

    - Loop 只调用 stream()，不感知具体模型 / harness。
    - options 透传 sampling 参数：temperature / top_p / max_tokens / response_format / stop 等。
    - v0 harness: 内部无 retry/fallback，只调一个模型。
    - 错误：网络 / API 错误抛异常，由 harness / Loop 上层捕获处理。
    """

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        ...