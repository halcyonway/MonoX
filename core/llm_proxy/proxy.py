"""LLM 流式调用实现。

依赖：httpx（async HTTP）。

模型特定参数通过 `cfg.extra_params` 透传。

Provider 支持：多个 provider 配置在 `config.toml [providers]`，
key=自定义名，value={model_real_name, base_url, apikey_env}。
stream() 调用时通过 options["model_provider"] 引用。
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from core.config import LLMConfig, ModelProvider
from core.protocol import LLMProxy as LLMProxyProto, LlmChunk

_log = logging.getLogger("monox.llm_proxy")


def _normalize_usage(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """把 OpenAI 风格的嵌套 usage 拍平，提取 cached_tokens 到顶层。"""
    if raw is None:
        return None
    out = dict(raw)
    details = out.pop("prompt_tokens_details", None) or {}
    if isinstance(details, dict) and "cached_tokens" in details:
        out["cached_tokens"] = details["cached_tokens"]
    return out


class LlmProxy(LLMProxyProto):
    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        # stream() 调用后更新：本次真正发给厂家 API 的 model 字段。
        # trace / metric 等下游消费者拿这个展示，避免和 cfg.model("人类可读名")混淆。
        self._last_model: str = cfg.model

    def _resolve(self, provider_name: str | None) -> tuple[str, str, str, int, dict[str, Any]]:
        """解析真实 base_url / api_key / model_real_name / timeout / extra_params。

        priority:
        1. options 里指定 provider_name → 查 cfg.providers
        2. cfg.provider_name → 查 cfg.providers
        3. 回退到 cfg.api_base / cfg.api_key / cfg.model（旧兼容）
        """
        providers = getattr(self._cfg, "providers", None) or {}

        if provider_name and provider_name in providers:
            p = providers[provider_name]
            api_key = os.environ.get(p.apikey_env, "")
            return p.base_url, api_key, p.model_real_name, p.timeout, p.extra_params
        if getattr(self._cfg, "provider_name", None) and self._cfg.provider_name in providers:
            p = providers[self._cfg.provider_name]
            api_key = os.environ.get(p.apikey_env, "")
            return p.base_url, api_key, p.model_real_name, p.timeout, p.extra_params
        return self._cfg.api_base, self._cfg.api_key, self._cfg.model, self._cfg.timeout, {}

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        prov_name = (options or {}).get("model_provider")
        base_url, api_key, model, timeout, extra_params = self._resolve(prov_name)
        # 缓存本次 stream 用的真实 model 名（provider 解析后）。trace 展示用：
        # 配置层 model（cfg.llm.model）是"人类可读名"，不一定等于厂家真实 model 字段；
        # span 必须记实际发给厂家 API 的那个字符串，否则跨 provider 时显示错。
        self._last_model = model
        # 容错：base_url 写成完整 endpoint（以 /chat/completions 结尾）也能用，
        # 剥掉后缀，后面统一拼 /chat/completions。否则会拼出双重路径 404。
        if base_url.rstrip("/").endswith("/chat/completions"):
            base_url = base_url.rstrip("/")[: -len("/chat/completions")]

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        # 合并顺序（后者覆盖前者）：
        #   extra_params → custom → cfg.options → 调用方 options
        payload.update(extra_params)
        payload.update(self._cfg.extra_params)
        payload.update(self._cfg.custom)
        payload.update(self._cfg.options)
        if options:
            payload.update(options)

        _log.info(
            "llm request model=%s base_url=%s apikey_env=%s stream_options=%s msgs=%d tools=%d",
            payload.get("model"),
            base_url,
            (options or {}).get("model_provider") or getattr(self._cfg, "provider_name", None) or "<inline>",
            payload.get("stream_options"),
            len(messages),
            len(tools) if tools else 0,
        )
        if not base_url or not api_key:
            _log.error(
                "llm config incomplete: model=%s base_url=%s api_key=%s",
                model, base_url or "<empty>", "<set>" if api_key else "<empty>",
            )

        client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            # connect 单独收紧：死域名 / 错误路径要快速失败，不能挂满整个 timeout
            timeout=httpx.Timeout(timeout, connect=min(10.0, float(timeout))),
        )
        chunk_count = 0
        usage_count = 0
        # TTFT 起点：进 async with 立刻打点；首次产出 delta_text / delta_reasoning
        # 非空 chunk 时计算 first_chunk_at_ms（monotonic，单位 ms）。
        request_start = time.monotonic()
        first_chunk_at_ms: float | None = None
        try:
            async with client.stream("POST", "/chat/completions", json=payload) as resp:
                # 非 200：把响应 body 读出来打进日志（厂家错误详情都在 body 里）
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    _log.error("llm http error status=%d url=%s body=%s", resp.status_code, resp.url, body[:2000])
                    resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    chunk = self._parse_chunk(json.loads(data))
                    _log.debug("raw chunk usage=%r", chunk.usage)
                    chunk_count += 1
                    if chunk.usage:
                        usage_count += 1
                    # 首个非空 content/reasoning chunk：填上 first_chunk_at_ms。
                    # 注意：tool_calls delta 不算"首 token"（不消耗 output token）。
                    if first_chunk_at_ms is None and (chunk.delta_text or chunk.delta_reasoning):
                        first_chunk_at_ms = (time.monotonic() - request_start) * 1000.0
                        chunk = LlmChunk(
                            delta_text=chunk.delta_text,
                            delta_reasoning=chunk.delta_reasoning,
                            delta_tool_calls=chunk.delta_tool_calls,
                            finish_reason=chunk.finish_reason,
                            usage=chunk.usage,
                            first_chunk_at_ms=first_chunk_at_ms,
                        )
                    yield chunk
        except Exception:
            _log.exception("llm stream failed: model=%s base_url=%s", model, base_url)
            raise
        finally:
            await client.aclose()

        _log.info(
            "llm response model=%s chunks=%d chunks_with_usage=%d",
            model,
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
