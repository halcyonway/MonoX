"""Trace 数据模型：Run / Turn / Span。

frozen dataclass，纯数据；可 JSON 序列化。

属性键命名空间遵循 OTel Semantic Conventions（详见
`core/observability/otel_attrs.py` 和 `spec/requirements/observability-otel.md`）。
本文件只定义数据形状，不规定属性名（属性名常量在 otel_attrs.py）。

SpanKind 分类：
- Phase（每 run 固定各 1 个）：BOOTSTRAP / LOOP / FINALIZE
- Logical container：TURN
- Work：REASONING / ACT / TOOL / COMPRESS

parent_id 真层级（之前是假的——所有 span 都指向 turn_id）：
- bootstrap / loop / finalize  →  parent_id = run_id
- turn                          →  parent_id = loop_span_id
- reasoning / act / compress    →  parent_id = turn_span_id
- tool                          →  parent_id = act_span_id（若 act 不存在则 turn_span_id）
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal


# schema_version：v1 = 旧（ad-hoc snake_case），v2 = OTel 语义 + 嵌套树
SCHEMA_VERSION_CURRENT = 2


class SpanKind(str, Enum):
    # ── Phase spans（每 run 固定各 1 个）──
    BOOTSTRAP = "bootstrap"   # engine.run() 启动到第一个 user event
    LOOP = "loop"             # 包裹所有 _react 迭代
    FINALIZE = "finalize"     # end_run 起，output queue 排干
    # ── Logical container ──
    TURN = "turn"             # 一个 react step（每 _react iteration 一个）
    # ── Work ──
    REASONING = "reasoning"   # 单次 LLM 调用
    ACT = "act"               # 一个 turn 里所有 tool calls 的容器（仅 tool_calls > 0 时发）
    TOOL = "tool"             # 单次 tool 调用
    COMPRESS = "compress"     # L1/L2 fold


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
    """Per-turn grouping container。turn_id == 该 turn 下 TURN span 的 span_id。

    Turn.spans 包含该 turn 子树的所有 spans（TURN + REASONING + ACT + TOOL +
    COMPRESS），parent_id 描述实际嵌套关系。
    """
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
    schema_version: int = SCHEMA_VERSION_CURRENT
    # run-level spans（bootstrap / loop / finalize，parent_id == run_id）
    spans: tuple[Span, ...] = ()
    # per-turn spans（TURN + REASONING + ACT + TOOL + COMPRESS，parent_id 描述嵌套）
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
            "schema_version": self.schema_version,
            "spans": [s.to_dict() for s in self.spans],
            "turns": [t.to_dict() for t in self.turns],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Run":
        # 旧 v1 缺 schema_version 字段 → 标 1；caller 用 run.schema_version < 2 识别 v1
        return cls(
            run_id=raw["run_id"],
            session_key=raw["session_key"],
            user_text=raw.get("user_text", ""),
            final_text=raw.get("final_text"),
            start_ts=float(raw["start_ts"]),
            end_ts=float(raw["end_ts"]) if raw.get("end_ts") is not None else None,
            status=raw.get("status", "running"),
            schema_version=int(raw.get("schema_version", 1)),
            spans=tuple(Span.from_dict(s) for s in raw.get("spans") or ()),
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
        schema_version=SCHEMA_VERSION_CURRENT,
        spans=(),
        turns=(),
    )


def new_turn(turn_idx: int) -> Turn:
    return Turn(turn_id=_new_id("u"), turn_idx=turn_idx, spans=())