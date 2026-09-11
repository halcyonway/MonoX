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
    # 本地绝对路径（如果 attachment 是文件落地到本地的，比如音频）。
    # 优先级：path > content（content 里存 URL 字符串是历史兼容路径，
    # event_format 会先看 path 再看 content）。
    path: str | None = None


# ---------- Gateway → Loop ----------

@dataclass(frozen=True)
class InboundEvent:
    session_key: str
    # kind 是传输层载荷类型（封闭枚举，engine 分流依据）：interrupt 走高优先级
    # 中断队列，其余全部进 sub_queue 当消息聚合。system = runtime 内部产生的
    # 通知（如 async-task-result），非用户输入——语义区分靠 event_type 贴标签。
    kind: Literal["message", "interrupt", "command", "attachment", "system"]
    text: str
    source: str = "default"           # 来源标识：channel 名或其他信号源
    event_type: str = "user-input"   # 事件类型：user-input / scheduled-task / system-notify / async-task-result 等
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
    # call_id: OpenAI tool call id，用于把 ToolPending（早些发的）和 ToolStart 配对，
    # 避免前端出现「pending 块 + tool_start 又创一个」的双块。
    call_id: str = ""


@dataclass(frozen=True)
class ToolPending:
    """LLM 流式响应里**第一次**看到某个 tool call 的 id+name 时立即发。

    原 ToolStart 要等整段 args JSON 全部收到 + 解析完成才 fire，所以前端 tool block
    要等很久才出现。ToolPending 是「开始调用了」信号，前端立刻展示 loading 态；
    args 出完后再来 ToolStart 补上完整参数（同一个 call_id），前端 update 而非新建。

    - call_id: 与后续 ToolStart 对应的 OpenAI tool call id
    - name: function name（streaming 第一个 delta 通常就 set 了）
    - tool_index: 同 turn 内的并行 tool call 序号（OpenAI streaming format 字段）
    - args_so_far: 已经流到的部分 arguments JSON，前端可以做"参数预览中"展示
    """
    call_id: str
    name: str
    tool_index: int
    args_so_far: str


@dataclass(frozen=True)
class ToolEnd:
    name: str
    result: "ToolResult"
    latency_ms: int


@dataclass(frozen=True)
class StatusChange:
    state: Literal["thinking", "tooling", "compressing", "wait_io", "idle"]
    # 可观测性：当前 turn 所属 run / turn。客户端可忽略。
    trace_id: str | None = None
    turn_id: str | None = None


@dataclass(frozen=True)
class MetricChunk:
    metrics: dict[str, Any]
    # 可观测性：metric chunk 所属 run / turn。
    trace_id: str | None = None
    turn_id: str | None = None
    # 本次 LLM 调用真实使用的 model 名（provider 解析后），跨 provider 切换时 UI 据此刷新。
    model: str | None = None


@dataclass(frozen=True)
class FinalMessage:
    text: str
    metrics: dict[str, Any] = field(default_factory=dict)
    # 可观测性：本次 reply 所属 run。客户端可挂"看 trace"按钮。
    trace_id: str | None = None


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
    ToolPending,
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
#
# 不再有 CheckpointRecord dataclass——checkpoint 改 append-only log 模式
# （见 core/protocol/storage.py 的 CheckpointStore 文档）。events 直接是 dict：
#   {"kind": "msg"|"compact", ...}
# 两类节点。