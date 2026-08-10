"""OpenAI-compatible stream 实现。

依赖：httpx（async HTTP）。
v0: 单模型 + 简单调用，无 retry/fallback（harness 后续在 LLMProxy 内加）。

模型特定参数（如 MiniMax 的 `reasoning_split`）通过 `cfg.extra_params` 透传，
用户在 config.toml `[llm.extra_params]` 配置。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from core.config import LLMConfig
from core.protocol import LLMProxy, LlmChunk


class OpenAIStreamProxy(LLMProxy):
    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.api_base,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=cfg.timeout,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        payload: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        # 合并顺序（后者覆盖前者）：
        #   extra_params → cfg.options → 调用方 options
        payload.update(self._cfg.extra_params)
        payload.update(self._cfg.options)
        if options:
            payload.update(options)

        async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                yield self._parse_chunk(json.loads(data))

    @staticmethod
    def _parse_chunk(chunk: dict[str, Any]) -> LlmChunk:
        choices = chunk.get("choices") or []
        if not choices:
            return LlmChunk(usage=chunk.get("usage"))
        delta = choices[0].get("delta") or {}
        finish = choices[0].get("finish_reason")
        usage = chunk.get("usage")
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        return LlmChunk(
            delta_text=delta.get("content"),
            delta_reasoning=reasoning,
            delta_tool_calls=tuple(delta.get("tool_calls") or ()),
            finish_reason=finish,
            usage=usage,
        )

    async def aclose(self) -> None:
        await self._client.aclose()