"""TraceCollector — 挂在 LoopEngine 上的被动收集器。

接口是 fire-and-forget 的 async 调用；内部维护一个 in-memory 的 Run（layer of
truth），每次有事件就 upsert 一次（保留 span / turn 不可变，用 frozen dataclass
的"复制-替换"模式），end_run 时一次性 flush 到 store。

`LoopEngine` 必须全程用 `if self._traces:` 守卫——`traces=None` 时整个收集路径
不执行任何代码（包括属性访问）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from core.observability.store import TraceStore
from core.observability.types import (
    Run,
    Span,
    SpanKind,
    Turn,
    new_run,
    new_turn,
)

_log = logging.getLogger("monox.trace.collector")


def _now() -> float:
    return time.time()


def _replace_turn(run: Run, turn: Turn) -> Run:
    """frozen Run 不可变：替换 turns 元组里 idx 相同的那一条。"""
    out: list[Turn] = []
    replaced = False
    for t in run.turns:
        if t.turn_id == turn.turn_id and not replaced:
            out.append(turn)
            replaced = True
        else:
            out.append(t)
    if not replaced:
        out.append(turn)
    return Run(
        run_id=run.run_id,
        session_key=run.session_key,
        user_text=run.user_text,
        final_text=run.final_text,
        start_ts=run.start_ts,
        end_ts=run.end_ts,
        status=run.status,
        turns=tuple(out),
    )


def _append_span(turn: Turn, span: Span) -> Turn:
    return Turn(
        turn_id=turn.turn_id,
        turn_idx=turn.turn_idx,
        spans=turn.spans + (span,),
    )


def _replace_span(turn: Turn, span: Span) -> Turn:
    out = []
    replaced = False
    for s in turn.spans:
        if s.span_id == span.span_id and not replaced:
            out.append(span)
            replaced = True
        else:
            out.append(s)
    if not replaced:
        out.append(span)
    return Turn(
        turn_id=turn.turn_id,
        turn_idx=turn.turn_idx,
        spans=tuple(out),
    )


class TraceCollector:
    """被动 trace 收集器。

    用法：
        collector = TraceCollector(store, session_key)
        await collector.begin_run(user_text)
        turn_id = await collector.begin_turn(0)
        await collector.record_llm_span(turn_id, ...)
        await collector.end_run(final_text, status="ok")
    """

    def __init__(self, store: TraceStore, session_key: str) -> None:
        self._store = store
        self._session_key = session_key
        self._lock = asyncio.Lock()
        self._current: Run | None = None
        self._turns: dict[str, Turn] = {}

    # ---- run lifecycle ----

    async def begin_run(self, user_text: str) -> str:
        async with self._lock:
            run = new_run(self._session_key, user_text)
            self._current = run
            self._turns.clear()
            return run.run_id

    async def end_run(self, final_text: str | None, status: str = "ok") -> None:
        async with self._lock:
            run = self._current
            if run is None:
                return
            self._current = Run(
                run_id=run.run_id,
                session_key=run.session_key,
                user_text=run.user_text,
                final_text=final_text,
                start_ts=run.start_ts,
                end_ts=_now(),
                status=status,  # type: ignore[arg-type]
                turns=run.turns,
            )
            await self._store.save_run(self._current)

    @property
    def current_run_id(self) -> str | None:
        return self._current.run_id if self._current else None

    @property
    def current_turn_id(self) -> str | None:
        # 最近的 turn
        if not self._turns:
            return None
        return next(reversed(self._turns))

    # ---- turn lifecycle ----

    async def begin_turn(self, turn_idx: int) -> str:
        async with self._lock:
            turn = new_turn(turn_idx)
            self._turns[turn.turn_id] = turn
            if self._current is not None:
                self._current = _replace_turn(self._current, turn)
            return turn.turn_id

    # ---- span recorders ----

    async def add_span(self, turn_id: str, span: Span) -> None:
        async with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                _log.warning("add_span: turn_id=%s not found", turn_id)
                return
            if span.end_ts is None:
                span = span.close()
            turn = _append_span(turn, span)
            self._turns[turn_id] = turn
            if self._current is not None:
                self._current = _replace_turn(self._current, turn)

    async def record_llm_span(
        self,
        turn_id: str,
        *,
        model: str,
        messages: list[dict[str, Any]],
        response_text: str,
        reasoning_content: str | None,
        usage: dict[str, Any] | None,
        finish_reason: str | None,
        latency_ms: int,
        status: str = "ok",
    ) -> str:
        attributes = {
            "model": model,
            "messages": messages,
            "response_text": response_text,
            "reasoning_content": reasoning_content,
            "usage": usage or {},
            "finish_reason": finish_reason,
            "latency_ms": latency_ms,
        }
        span = Span.now_span(
            kind=SpanKind.REASONING,
            name=f"reasoning:{model}",
            parent_id=turn_id,
            attributes=attributes,
            status=status,  # type: ignore[arg-type]
        )
        await self.add_span(turn_id, span)
        return span.span_id

    async def record_act_span(
        self,
        turn_id: str,
        *,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any] | None,
        latency_ms: int,
        status: str = "ok",
    ) -> str:
        attributes = {
            "tool_name": tool_name,
            "args": args,
            "result": result or {},
            "latency_ms": latency_ms,
        }
        span = Span.now_span(
            kind=SpanKind.ACT,
            name=f"act:{tool_name}",
            parent_id=turn_id,
            attributes=attributes,
            status=status,  # type: ignore[arg-type]
        )
        await self.add_span(turn_id, span)
        return span.span_id

    async def record_compress_span(
        self,
        turn_id: str,
        *,
        level: str,
        summary: str,
        folded_count: int,
        budget_ids: list[str] | None = None,
        status: str = "ok",
    ) -> str:
        attributes = {
            "level": level,
            "summary": summary,
            "folded_count": folded_count,
            "budget_ids": list(budget_ids or []),
        }
        span = Span.now_span(
            kind=SpanKind.COMPRESS,
            name=f"compress:{level}",
            parent_id=turn_id,
            attributes=attributes,
            status=status,  # type: ignore[arg-type]
        )
        await self.add_span(turn_id, span)
        return span.span_id