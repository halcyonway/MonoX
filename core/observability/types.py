"""Trace 数据模型：Run / Turn / Span。

frozen dataclass，纯数据；可 JSON 序列化；属性字典用约定子键：
- REASONING: model, messages, response_text, reasoning_content, usage, finish_reason, latency_ms
- ACT:       tool_name, args, result, latency_ms
- COMPRESS:  level, summary, folded_count, budget_ids
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal


class SpanKind(str, Enum):
    REASONING = "reasoning"
    ACT = "act"
    COMPRESS = "compress"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Span:
    span_id: str
    parent_id: str | None
    kind: SpanKind
    name: str
    start_ts: float
    end_ts: float | None
    status: Literal["ok", "error", "cancelled"] = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Span":
        return cls(
            span_id=raw["span_id"],
            parent_id=raw.get("parent_id"),
            kind=SpanKind(raw["kind"]),
            name=raw["name"],
            start_ts=float(raw["start_ts"]),
            end_ts=float(raw["end_ts"]) if raw.get("end_ts") is not None else None,
            status=raw.get("status", "ok"),
            attributes=dict(raw.get("attributes") or {}),
        )

    @staticmethod
    def now_span(
        kind: SpanKind,
        name: str,
        parent_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        status: Literal["ok", "error", "cancelled"] = "ok",
    ) -> "Span":
        return Span(
            span_id=_new_id("s"),
            parent_id=parent_id,
            kind=kind,
            name=name,
            start_ts=time.time(),
            end_ts=None,
            status=status,
            attributes=attributes or {},
        )

    def close(self, status: Literal["ok", "error", "cancelled"] | None = None) -> "Span":
        return Span(
            span_id=self.span_id,
            parent_id=self.parent_id,
            kind=self.kind,
            name=self.name,
            start_ts=self.start_ts,
            end_ts=time.time(),
            status=status if status is not None else self.status,
            attributes=self.attributes,
        )


@dataclass(frozen=True)
class Turn:
    turn_id: str
    turn_idx: int
    spans: tuple[Span, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "turn_idx": self.turn_idx,
            "spans": [s.to_dict() for s in self.spans],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Turn":
        return cls(
            turn_id=raw["turn_id"],
            turn_idx=int(raw["turn_idx"]),
            spans=tuple(Span.from_dict(s) for s in raw.get("spans") or ()),
        )


@dataclass(frozen=True)
class Run:
    run_id: str
    session_key: str
    user_text: str
    final_text: str | None
    start_ts: float
    end_ts: float | None
    status: Literal["running", "ok", "error", "cancelled"] = "running"
    turns: tuple[Turn, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_key": self.session_key,
            "user_text": self.user_text,
            "final_text": self.final_text,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "status": self.status,
            "turns": [t.to_dict() for t in self.turns],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Run":
        return cls(
            run_id=raw["run_id"],
            session_key=raw["session_key"],
            user_text=raw.get("user_text", ""),
            final_text=raw.get("final_text"),
            start_ts=float(raw["start_ts"]),
            end_ts=float(raw["end_ts"]) if raw.get("end_ts") is not None else None,
            status=raw.get("status", "running"),
            turns=tuple(Turn.from_dict(t) for t in raw.get("turns") or ()),
        )


def new_run(session_key: str, user_text: str) -> Run:
    return Run(
        run_id=_new_id("t"),
        session_key=session_key,
        user_text=user_text,
        final_text=None,
        start_ts=time.time(),
        end_ts=None,
        status="running",
        turns=(),
    )


def new_turn(turn_idx: int) -> Turn:
    return Turn(turn_id=_new_id("u"), turn_idx=turn_idx, spans=())