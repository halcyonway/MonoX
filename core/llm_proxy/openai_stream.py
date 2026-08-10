"""OpenAI-compatible stream 实现。

依赖：httpx（async HTTP）。
v0: 单模型 + 简单调用，无 retry/fallback（harness 后续在 LLMProxy 内加）。

MiniMax / DeepSeek-R1 等 reasoning 模型：
- 默认把推理塞进 content（`<think>...</think>`），污染输出
- 加 `reasoning_split: true` 参数让 API 自动分离到 `reasoning_content` 字段
- 我们从 delta 提取 `reasoning_content` → LlmChunk.delta_reasoning，channel 决定是否显示
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
            # 默认启用 reasoning 分离（MiniMax / DeepSeek-R1 等 thinking 模型）
            "reasoning_split": True,
        }
        if tools:
            payload["tools"] = tools
        # cfg.options 作默认值，调用方 options 覆盖（用户可关掉 reasoning_split）
        merged = {**(self._cfg.options or {}), **(options or {})}
        payload.update(merged)

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