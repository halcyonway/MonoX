"""TraceCollector — 挂在 LoopEngine 上的被动收集器。

接口是 fire-and-forget 的 async 调用；内部维护一个 in-memory 的 Run（layer of
truth），每次有事件就 upsert 一次（保留 span / turn 不可变，用 frozen dataclass
的"复制-替换"模式），end_run 时一次性 flush 到 store。

`LoopEngine` 必须全程用 `if self._traces:` 守卫——`traces=None` 时整个收集路径
不执行任何代码（包括属性访问）。

Span 树形状（参见 `core/observability/types.py` docstring 与
`spec/requirements/observability-otel.md`）：
- run-level spans（bootstrap / loop / finalize，parent_id == run_id）→ Run.spans
- turn spans（TURN，parent_id == loop_span_id）→ Turn.spans
- work spans（reasoning / act / compress，parent_id == turn_span_id）→ Turn.spans
- tool spans（parent_id == act_span_id 或 turn_span_id）→ Turn.spans

属性键全部走 OTel Semantic Convention（`core/observability/otel_attrs.py`）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Literal

from core.observability.otel_attrs import (
    ATTR_ERROR_MESSAGE,
    ATTR_ERROR_TYPE,
    ATTR_GENAI_CLIENT_OPERATION_DURATION,
    ATTR_GENAI_CLIENT_TIME_TO_FIRST_TOKEN,
    ATTR_GENAI_REQUEST_MESSAGES,
    ATTR_GENAI_REQUEST_MODEL,
    ATTR_GENAI_REQUEST_TOOL_SPECS,
    ATTR_GENAI_RESPONSE_FINISH_REASONS,
    ATTR_GENAI_RESPONSE_MODEL,
    ATTR_GENAI_RESPONSE_REASONING,
    ATTR_GENAI_RESPONSE_TEXT,
    ATTR_GENAI_USAGE_CACHED_TOKENS,
    ATTR_GENAI_USAGE_INPUT_TOKENS,
    ATTR_GENAI_USAGE_OUTPUT_TOKENS,
    ATTR_LOOP_CANCELLED,
    ATTR_LOOP_COMPRESS_BUDGETS,
    ATTR_LOOP_COMPRESS_FOLDED,
    ATTR_LOOP_COMPRESS_LEVEL,
    ATTR_LOOP_COMPRESS_SUMMARY,
    ATTR_LOOP_TURN_IDX,
    ATTR_SERVICE_NAME,
    ATTR_TOOL_CALL_ARGUMENTS,
    ATTR_TOOL_CALL_ID,
    ATTR_TOOL_NAME,
    ATTR_TOOL_RESULT,
    ATTR_TOOL_RESULT_STATUS,
    ATTR_TOOL_RESULT_TRUNCATED,
)
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


# ──────────── 不可变 Run / Turn 增量更新工具 ────────────


def _replace_turn(run: Run, turn: Turn) -> Run:
    """frozen Run 不可变：替换 turns 元组里 turn_id 相同的那一条。"""
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
        schema_version=run.schema_version,
        spans=run.spans,
        turns=tuple(out),
    )


def _append_run_span(run: Run, span: Span) -> Run:
    return Run(
        run_id=run.run_id,
        session_key=run.session_key,
        user_text=run.user_text,
        final_text=run.final_text,
        start_ts=run.start_ts,
        end_ts=run.end_ts,
        status=run.status,
        schema_version=run.schema_version,
        spans=run.spans + (span,),
        turns=run.turns,
    )


def _replace_run_span(run: Run, span: Span) -> Run:
    out: list[Span] = []
    replaced = False
    for s in run.spans:
        if s.span_id == span.span_id and not replaced:
            out.append(span)
            replaced = True
        else:
            out.append(s)
    if not replaced:
        out.append(span)
    return Run(
        run_id=run.run_id,
        session_key=run.session_key,
        user_text=run.user_text,
        final_text=run.final_text,
        start_ts=run.start_ts,
        end_ts=run.end_ts,
        status=run.status,
        schema_version=run.schema_version,
        spans=tuple(out),
        turns=run.turns,
    )


def _append_turn_span(turn: Turn, span: Span) -> Turn:
    return Turn(
        turn_id=turn.turn_id,
        turn_idx=turn.turn_idx,
        spans=turn.spans + (span,),
    )


def _replace_turn_span(turn: Turn, span: Span) -> Turn:
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

    用法（典型）：
        collector = TraceCollector(store, session_key)
        run_id = await collector.begin_run(user_text)
        bs_id = await collector.begin_span(parent_id=run_id, kind=SpanKind.BOOTSTRAP, name="bootstrap")
        ... # 启动 / restore
        await collector.end_span(bs_id)
        loop_id = await collector.begin_span(parent_id=run_id, kind=SpanKind.LOOP, name="loop")
        turn_id = await collector.begin_turn(0)            # 内部发 TURN span
        span_id = await collector.record_llm_span(turn_id, ...)
        await collector.record_tool_span(act_id, ...)
        await collector.end_span(turn_id)
        await collector.end_span(loop_id)
        await collector.end_run(final_text, status="ok")
    """

    def __init__(self, store: TraceStore, session_key: str) -> None:
        self._store = store
        self._session_key = session_key
        self._lock = asyncio.Lock()
        self._current: Run | None = None
        # 记录每个 turn 的 act 容器 span_id（如果有）→ tool span 用它当 parent
        self._turns: dict[str, Turn] = {}
        self._turn_act_span: dict[str, str] = {}  # turn_id -> act_span_id

    # ---- run lifecycle ----

    async def begin_run(self, user_text: str) -> str:
        async with self._lock:
            run = new_run(self._session_key, user_text)
            self._current = run
            self._turns.clear()
            self._turn_act_span.clear()
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
                schema_version=run.schema_version,
                spans=run.spans,
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

    # ---- generic span (run-level phase + finalize) ----

    async def begin_span(
        self,
        parent_id: str,
        *,
        kind: SpanKind,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        """开始一个 run-level span（bootstrap / loop / finalize）或 turn-level
        span（turn 容器本身）；返回 span_id。

        目前 turn-level 也用这条路径——begin_turn 内部就是它。
        """
        async with self._lock:
            span = Span.now_span(
                kind=kind,
                name=name,
                parent_id=parent_id,
                attributes=attributes or {},
            )
            if self._current is None:
                _log.warning("begin_span without active run: kind=%s name=%s", kind, name)
                return span.span_id
            # run-level：bootstrap/loop/finalize
            if parent_id == self._current.run_id:
                self._current = _append_run_span(self._current, span)
            else:
                # turn-level
                turn = self._turns.get(parent_id)
                if turn is None:
                    _log.warning("begin_span: parent_id=%s not found (kind=%s)", parent_id, kind)
                    return span.span_id
                turn = _append_turn_span(turn, span)
                self._turns[parent_id] = turn
                self._current = _replace_turn(self._current, turn)
            return span.span_id

    async def end_span(
        self,
        span_id: str,
        *,
        status: Literal["ok", "error", "cancelled"] = "ok",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            if self._current is None:
                return
            # 在 run.spans 里找
            for s in self._current.spans:
                if s.span_id == span_id:
                    merged = s.close(status=status)
                    if attributes:
                        merged = Span(
                            span_id=merged.span_id,
                            parent_id=merged.parent_id,
                            kind=merged.kind,
                            name=merged.name,
                            start_ts=merged.start_ts,
                            end_ts=merged.end_ts,
                            status=merged.status,
                            attributes={**merged.attributes, **attributes},
                        )
                    self._current = _replace_run_span(self._current, merged)
                    return
            # 在 turn.spans 里找
            for turn_id, turn in self._turns.items():
                for s in turn.spans:
                    if s.span_id == span_id:
                        merged = s.close(status=status)
                        if attributes:
                            merged = Span(
                                span_id=merged.span_id,
                                parent_id=merged.parent_id,
                                kind=merged.kind,
                                name=merged.name,
                                start_ts=merged.start_ts,
                                end_ts=merged.end_ts,
                                status=merged.status,
                                attributes={**merged.attributes, **attributes},
                            )
                        turn = _replace_turn_span(turn, merged)
                        self._turns[turn_id] = turn
                        self._current = _replace_turn(self._current, turn)
                        return
            _log.warning("end_span: span_id=%s not found", span_id)

    # ---- turn lifecycle ----

    async def begin_turn(self, turn_idx: int) -> str:
        """开始一个新 turn：发一个 kind=turn 的容器 span，turn_id == span_id。

        turn_id 与该 turn 下 TURN span 的 span_id 用同一个 ID（统一 `s_` 前缀），
        这样下游 record_* 助手把 turn_id 当 parent_id 传给子 span（ACT / TOOL /
        REASONING / COMPRESS）时，parent_id 跟 TURN span.span_id 直接相等，
        parent_id 链能跑通。

        parent_id 默认走当前最近的 loop span；如果还没 begin loop span，则用
        current_run_id 作 fallback（这样 trace 不会断）。
        """
        async with self._lock:
            # 找最近的 loop span
            loop_id = self._find_loop_span_id()
            parent_id = loop_id or (self._current.run_id if self._current else None)
            if parent_id is None:
                _log.warning("begin_turn: no active run/loop span")
                return ""
            # 先建 span（决定 span_id = turn_id）
            span = Span.now_span(
                kind=SpanKind.TURN,
                name=f"turn:{turn_idx}",
                parent_id=parent_id,
                attributes={ATTR_LOOP_TURN_IDX: turn_idx, ATTR_SERVICE_NAME: "monox"},
            )
            # 用 span_id 统一当 turn_id（turn.spans[0] 的 span_id）
            turn = Turn(
                turn_id=span.span_id,
                turn_idx=turn_idx,
                spans=(span,),
            )
            self._turns[turn.turn_id] = turn
            self._turn_act_span.pop(turn.turn_id, None)
            if self._current is not None:
                self._current = _replace_turn(self._current, turn)
            return turn.turn_id

    async def end_turn(self, turn_id: str, *, status: str = "ok") -> None:
        async with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                return
            # 关掉 TURN span（spans[0]）
            if turn.spans:
                tspan = turn.spans[0].close(status=status)  # type: ignore[arg-type]
                turn = Turn(
                    turn_id=turn.turn_id,
                    turn_idx=turn.turn_idx,
                    spans=(tspan, *turn.spans[1:]),
                )
                self._turns[turn_id] = turn
                if self._current is not None:
                    self._current = _replace_turn(self._current, turn)
            self._turn_act_span.pop(turn_id, None)

    def _find_loop_span_id(self) -> str | None:
        if self._current is None:
            return None
        for s in self._current.spans:
            if s.kind == SpanKind.LOOP and s.end_ts is None:
                return s.span_id
        return None

    # ---- span recorders (work spans) ----

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
        ttft_ms: int | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        status: Literal["ok", "error", "cancelled"] = "ok",
    ) -> str:
        """记录一次 reasoning span（OTel 语义属性键）。"""
        attributes: dict[str, Any] = {
            ATTR_GENAI_REQUEST_MODEL: model,
            ATTR_GENAI_REQUEST_MESSAGES: messages,
            ATTR_GENAI_REQUEST_TOOL_SPECS: list(tool_schemas or []),
            ATTR_GENAI_RESPONSE_MODEL: model,
            ATTR_GENAI_RESPONSE_TEXT: response_text,
            ATTR_GENAI_RESPONSE_REASONING: reasoning_content,
            ATTR_GENAI_RESPONSE_FINISH_REASONS: finish_reason,
            ATTR_GENAI_USAGE_INPUT_TOKENS: (usage or {}).get("prompt_tokens", 0),
            ATTR_GENAI_USAGE_OUTPUT_TOKENS: (usage or {}).get("completion_tokens", 0),
            ATTR_GENAI_USAGE_CACHED_TOKENS: (usage or {}).get("cached_tokens", 0),
            ATTR_GENAI_CLIENT_OPERATION_DURATION: latency_ms,
        }
        if ttft_ms is not None:
            attributes[ATTR_GENAI_CLIENT_TIME_TO_FIRST_TOKEN] = ttft_ms
        if error_type:
            attributes[ATTR_ERROR_TYPE] = error_type
        if error_message:
            attributes[ATTR_ERROR_MESSAGE] = error_message
        if status == "cancelled":
            attributes[ATTR_LOOP_CANCELLED] = True

        span = Span.now_span(
            kind=SpanKind.REASONING,
            name=f"reasoning:{model}",
            parent_id=turn_id,
            attributes=attributes,
            status=status,
        )
        await self._append_turn_span(turn_id, span)
        return span.span_id

    async def record_act_span(
        self,
        turn_id: str,
        *,
        tool_calls_count: int,
        status: Literal["ok", "error", "cancelled"] = "ok",
    ) -> str:
        """记录 ACT 容器 span（一个 turn 里所有 tool calls 的容器）。

        返回 act_span_id——后续 record_tool_span 用它当 parent。
        """
        attributes: dict[str, Any] = {
            ATTR_TOOL_CALL_ARGUMENTS: json.dumps({"count": tool_calls_count}, ensure_ascii=False),
        }
        span = Span.now_span(
            kind=SpanKind.ACT,
            name=f"act:{tool_calls_count}",
            parent_id=turn_id,
            attributes=attributes,
            status=status,
        )
        await self._append_turn_span(turn_id, span)
        self._turn_act_span[turn_id] = span.span_id
        return span.span_id

    async def record_tool_span(
        self,
        turn_id: str,
        *,
        tool_name: str,
        call_id: str,
        args: dict[str, Any] | None,
        result: dict[str, Any] | None,
        latency_ms: int,
        artifacts: dict[str, Any] | None = None,
        status: Literal["ok", "error", "cancelled"] = "ok",
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> str:
        """记录 TOOL span（OTel 语义属性键：tool.* + 嵌套 dict 序列化为 JSON 字符串）。

        parent_id 优先用 act_span_id（如果有），否则直接挂 turn_id 下。
        """
        parent_id = self._turn_act_span.get(turn_id) or turn_id
        # 按 OTel 约定，args / result 是 string（JSON 序列化）
        attributes: dict[str, Any] = {
            ATTR_TOOL_NAME: tool_name,
            ATTR_TOOL_CALL_ID: call_id,
            ATTR_TOOL_CALL_ARGUMENTS: json.dumps(args or {}, ensure_ascii=False, default=str),
            ATTR_TOOL_RESULT: json.dumps(result or {}, ensure_ascii=False, default=str),
            ATTR_GENAI_CLIENT_OPERATION_DURATION: latency_ms,
        }
        # result 状态单独提（OTel 没独立字段）
        if isinstance(result, dict):
            if "status" in result:
                attributes[ATTR_TOOL_RESULT_STATUS] = str(result.get("status"))
            if result.get("truncated"):
                attributes[ATTR_TOOL_RESULT_TRUNCATED] = True
        if error_type:
            attributes[ATTR_ERROR_TYPE] = error_type
        if error_message:
            attributes[ATTR_ERROR_MESSAGE] = error_message
        if status == "cancelled":
            attributes[ATTR_LOOP_CANCELLED] = True
        # artifacts 不走 OTel key；放在 attributes 里加前缀
        if artifacts:
            for k, v in artifacts.items():
                attributes[f"loop.tool.artifact.{k}"] = v

        span = Span.now_span(
            kind=SpanKind.TOOL,
            name=f"tool:{tool_name}",
            parent_id=parent_id,
            attributes=attributes,
            status=status,
        )
        await self._append_turn_span(turn_id, span)
        return span.span_id

    async def record_compress_span(
        self,
        turn_id: str,
        *,
        level: str,
        summary: str,
        folded_count: int,
        budget_ids: list[str] | None = None,
        status: Literal["ok", "error", "cancelled"] = "ok",
    ) -> str:
        """记录 compress span（OTel 扩展字段 loop.compress.*）。"""
        attributes: dict[str, Any] = {
            ATTR_LOOP_COMPRESS_LEVEL: level,
            ATTR_LOOP_COMPRESS_SUMMARY: summary,
            ATTR_LOOP_COMPRESS_FOLDED: folded_count,
            ATTR_LOOP_COMPRESS_BUDGETS: list(budget_ids or []),
        }
        span = Span.now_span(
            kind=SpanKind.COMPRESS,
            name=f"compress:{level}",
            parent_id=turn_id,
            attributes=attributes,
            status=status,
        )
        await self._append_turn_span(turn_id, span)
        return span.span_id

    # ---- 内部 ----

    async def _append_turn_span(self, turn_id: str, span: Span) -> None:
        """append 一个 span 到指定 turn；不存在就 warn 丢掉。"""
        async with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                _log.warning("_append_turn_span: turn_id=%s not found", turn_id)
                return
            if span.end_ts is None:
                span = span.close()
            turn = _append_turn_span(turn, span)
            self._turns[turn_id] = turn
            if self._current is not None:
                self._current = _replace_turn(self._current, turn)
