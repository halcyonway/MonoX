"""OpenAI-compatible stream 实现。

依赖：httpx（async HTTP）。
v0: 单模型 + 简单调用，无 retry/fallback（harness 后续在 LLMProxy 内加）。

模型特定参数（如 MiniMax 的 `reasoning_split`）通过 `cfg.extra_params` 透传，
用户在 config.toml `[llm.extra_params]` 配置。
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from core.config import LLMConfig
from core.protocol import LLMProxy, LlmChunk

_log = logging.getLogger("monox.llm_proxy")


def _normalize_usage(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """把 OpenAI 风格的嵌套 usage 拍平，提取 cached_tokens 到顶层。

    输入格式（OpenAI chat completion stream final chunk）：
        {"prompt_tokens": N, "completion_tokens": M,
         "prompt_tokens_details": {"cached_tokens": K}}
    拍平后：
        {"prompt_tokens": N, "completion_tokens": M, "cached_tokens": K}
    缺字段就 None。
    """
    if raw is None:
        return None
    out = dict(raw)
    details = out.pop("prompt_tokens_details", None) or {}
    if isinstance(details, dict) and "cached_tokens" in details:
        out["cached_tokens"] = details["cached_tokens"]
    return out


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
        #   extra_params → custom → cfg.options → 调用方 options
        payload.update(self._cfg.extra_params)
        payload.update(self._cfg.custom)
        payload.update(self._cfg.options)
        if options:
            payload.update(options)

        # 调试日志：记录请求 payload 中的关键字段，方便排查 usage 没回传等问题。
        # stream_options.include_usage 是否被透传、messages 有几条、tools 有几个。
        _log.info(
            "llm request model=%s stream_options=%s msgs=%d tools=%d",
            payload.get("model"),
            payload.get("stream_options"),
            len(messages),
            len(tools) if tools else 0,
        )

        chunk_count = 0
        usage_count = 0
        async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                chunk = self._parse_chunk(json.loads(data))
                chunk_count += 1
                if chunk.usage:
                    usage_count += 1
                yield chunk

        _log.info(
            "llm response model=%s chunks=%d chunks_with_usage=%d",
            self._cfg.model,
            chunk_count,
            usage_count,
        )

    @staticmethod
    def _parse_chunk(chunk: dict[str, Any]) -> LlmChunk:
        choices = chunk.get("choices") or []
        if not choices:
            return LlmChunk(usage=_normalize_usage(chunk.get("usage")))
        delta = choices[0].get("delta") or {}
        finish = choices[0].get("finish_reason")
        usage = _normalize_usage(chunk.get("usage"))
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