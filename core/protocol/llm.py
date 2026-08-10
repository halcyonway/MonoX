"""LLMProxy 接口。Loop 不感知具体模型，只看到 LlmChunk 流。"""
from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable

from .events import LlmChunk


@runtime_checkable
class LLMProxy(Protocol):
    """闭环 stream 接口，对齐 OpenAI-compatible stream delta。

    v0: 内部只调一个模型。
    后续 harness: retry / fallback / 限流 / token 预算。
    """

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        ...