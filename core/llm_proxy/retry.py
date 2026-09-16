"""LLM stream 自动重试装饰器。

设计意图（详见 `spec/requirements/llm-error-recovery.md`）：

LLM provider 调用失败时（HTTP 4xx/5xx、连接超时、connection reset），不直接
abort run —— 自动重试 N 次（指数退避），重试耗尽由 caller 走"system note
反馈给 LLM 自我恢复"路径。重试包成 async generator helper，caller 用
`async for chunk in retry_stream(lambda: llm.stream(...)):` 替换原来的
`async for chunk in llm.stream(...):`。

**已经 yield 给 caller 的 delta 不会重新 yield** —— caller 已经把它们
emit 到 output_queue / frontend，重发会污染 UI。重试成功的第二次 stream
从"网络恢复后的下一个 chunk"继续（每次 retry 复用同 messages + 同 seed），
caller 把两次 chunk 简单拼起来用即可（语义上等于同 input 的同 response）。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from core.protocol import LlmChunk

_log = logging.getLogger("monox.llm_proxy.retry")

# 总尝试次数（含第一次）。第 1 次失败 → sleep 1s 重试；第 2 次失败 → sleep 2s；
# 第 3 次失败 → sleep 4s；最后 sleep 后还失败 → 抛最终异常给 caller（不自动恢复，
# 由 caller 决定是 system note 还是 abort）。
MAX_LLM_RETRIES = 3

# LLM 网络错 → retry。
# 不包括 ValueError / KeyError / TypeError（LLM proxy 解析 chunk 错，那不是网络问题）
_RETRY_EXCEPTIONS = (
    httpx.HTTPStatusError,    # HTTP 4xx/5xx（raise_for_status 触发）
    httpx.RequestError,       # 连接错、socket reset、TLS handshake
    httpx.TimeoutException,   # connect / read / pool timeout
    ConnectionError,          # python builtin 兜底
    OSError,                  # socket 资源耗尽
)


# stream_factory 返回 AsyncIterator[LlmChunk] 的 async callable。
# 必须是 factory（lambda / method）而不是直接 iterator —— 每次 retry 需要重新调
# factory 生成新 stream 对象，否则 httpx.AsyncClient 复用会报 "stream already consumed"。
StreamFactory = Callable[..., AsyncIterator[LlmChunk]]


async def retry_stream(stream_factory: StreamFactory, *args: Any, **kwargs: Any) -> AsyncIterator[LlmChunk]:
    """包 stream() 调用，失败重试 3 次（指数退避 1s/2s/4s）。

    用法：
        async for chunk in retry_stream(
            lambda: llm.stream(messages, tools=tool_schemas, options=opts),
        ):
            ...

    重试语义：
    - 每次 retry 重新调 stream_factory() 拿新 iterator（不能用 cached iter）
    - 已 yield 给 caller 的 delta **不会**重新 yield —— caller 已经 emit 过
    - 重试成功的第二次 stream 从网络恢复后的下一个 chunk 继续，caller 拼起来用即可
    - 3 次全部失败 → 抛最后一次异常（caller 走 system note 路径，不 abort run）
    """
    last_exc: BaseException | None = None
    for attempt in range(1, MAX_LLM_RETRIES + 1):
        try:
            async for chunk in stream_factory(*args, **kwargs):
                yield chunk
            return  # 整个 stream 正常结束（[DONE] / break / 自然结束）
        except _RETRY_EXCEPTIONS as exc:
            last_exc = exc
            if attempt >= MAX_LLM_RETRIES:
                break
            backoff = 2 ** (attempt - 1)  # 1s, 2s, 4s
            _log.warning(
                "llm stream attempt %d/%d failed: %s — retry in %ds",
                attempt, MAX_LLM_RETRIES, type(exc).__name__, backoff,
            )
            await asyncio.sleep(backoff)
            continue

    # 重试耗尽 —— 抛最后一次错
    assert last_exc is not None
    _log.error(
        "llm stream failed after %d attempts: %s",
        MAX_LLM_RETRIES, type(last_exc).__name__,
    )
    raise last_exc