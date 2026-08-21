"""所有层间事件 schema。

不可变事件 + 单向数据流 + 控制信号分离。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union


# ---------- 基础附件 ----------

@dataclass(frozen=True)
class File:
    name: str
    content: bytes = b""
    mime: str = "application/octet-stream"


# ---------- Gateway → Loop ----------

@dataclass(frozen=True)
class InboundEvent:
    session_key: str
    kind: Literal["message", "interrupt", "command", "attachment"]
    text: str
    source: str = "default"           # 来源标识：channel 名或其他信号源
    event_type: str = "user-input"   # 事件类型：user-input / scheduled-task / system-notify / command 等
    timestamp: float = 0.0             # Unix 时间戳（秒，浮点）
    attachments: tuple[File, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


# ---------- Loop → Gateway (输出流) ----------

@dataclass(frozen=True)
class TokenChunk:
    text: str


@dataclass(frozen=True)
class ReasoningChunk:
    """LLM 内部推理（o1 / DeepSeek-R1 等），channel 决定是否展示。"""
    text: str


@dataclass(frozen=True)
class ToolStart:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolEnd:
    name: str
    result: "ToolResult"
    latency_ms: int


@dataclass(frozen=True)
class StatusChange:
    state: Literal["thinking", "tooling", "compressing", "wait_io", "idle"]


@dataclass(frozen=True)
class MetricChunk:
    metrics: dict[str, Any]


@dataclass(frozen=True)
class FinalMessage:
    text: str
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Card:
    data: dict[str, Any]


@dataclass(frozen=True)
class ErrorEvent:
    code: str
    msg: str
    retryable: bool


StreamEvent = Union[
    TokenChunk,
    ReasoningChunk,
    ToolStart,
    ToolEnd,
    StatusChange,
    MetricChunk,
    FinalMessage,
    Card,
    ErrorEvent,
]


# ---------- Loop ↔ Sandbox ----------

@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    status: Literal["ok", "error", "timeout", "cancelled"]
    stdout: str
    stderr: str
    exit_code: int
    artifacts: tuple[File, ...] = ()
    truncated: bool = False
    budget_id: str | None = None


# ---------- Loop ↔ LLMProxy ----------

@dataclass(frozen=True)
class LlmChunk:
    delta_text: str | None = None
    delta_reasoning: str | None = None  # o1 / DeepSeek-R1 等内部推理
    delta_tool_calls: tuple[dict[str, Any], ...] | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


# ---------- Checkpoint ----------

@dataclass(frozen=True)
class CheckpointRecord:
    session_key: str
    step_idx: int
    messages: tuple[dict[str, Any], ...]
    tool_results: tuple[ToolResult, ...]
    compressed_snapshot: dict[str, Any] | None = None