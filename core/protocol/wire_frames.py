"""MonoX Runtime ↔ Gateway (↔ desktop client) WebSocket 帧格式 v1。

帧信封：
    {"v": 1, "type": <one_of_21>, "seq": <int>, "ts": <float>, "data": <object>}

21 个 `type` 字符串：
- 出站（Runtime → Gateway）：hello, status, token, reasoning, tool_pending, tool_start,
                                tool_end, metric, final, card, error,
                                async_task_created, async_task_event, async_task_status,
                                async_task_list, async_task_snapshot
- 入站（Gateway → Runtime）：user_input, command, interrupt,
                              async_task_cancel, async_task_list_query

async_task_* 是 wire 层专用帧（不进 StreamEvent，先例 hello），承载异步任务
（subagent 是经典场景）的状态 / 事件流，见 spec/requirements/async-task.md。

本模块是 wire 协议的唯一事实来源；runtime server、gateway、desktop client
三方共享。MonoDesk desktop client 端 spec 在另一个仓，但 wire 字段必须一字不差。
"""
from __future__ import annotations

import json
import time
from typing import Any

from core.protocol import (
    Card,
    ErrorEvent,
    File,
    FinalMessage,
    InboundEvent,
    MetricChunk,
    ReasoningChunk,
    StatusChange,
    StreamEvent,
    TokenChunk,
    ToolEnd,
    ToolPending,
    ToolResult,
    ToolStart,
)


PROTOCOL_VERSION = 1


class FrameType:
    """21 个 wire frame type 字符串。集中定义避免魔法值。"""

    HELLO = "hello"
    STATUS = "status"
    TOKEN = "token"
    REASONING = "reasoning"
    TOOL_PENDING = "tool_pending"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    METRIC = "metric"
    FINAL = "final"
    CARD = "card"
    ERROR = "error"
    USER_INPUT = "user_input"
    COMMAND = "command"
    INTERRUPT = "interrupt"
    # async task（wire 层专用，见 spec/requirements/async-task.md）
    ASYNC_TASK_CREATED = "async_task_created"
    ASYNC_TASK_EVENT = "async_task_event"
    ASYNC_TASK_STATUS = "async_task_status"
    ASYNC_TASK_LIST = "async_task_list"
    ASYNC_TASK_SNAPSHOT = "async_task_snapshot"
    ASYNC_TASK_CANCEL = "async_task_cancel"
    ASYNC_TASK_LIST_QUERY = "async_task_list_query"


INBOUND_TYPES: frozenset[str] = frozenset({
    FrameType.USER_INPUT,
    FrameType.COMMAND,
    FrameType.INTERRUPT,
})

OUTBOUND_TYPES: frozenset[str] = frozenset({
    FrameType.HELLO,
    FrameType.STATUS,
    FrameType.TOKEN,
    FrameType.REASONING,
    FrameType.TOOL_PENDING,
    FrameType.TOOL_START,
    FrameType.TOOL_END,
    FrameType.METRIC,
    FrameType.FINAL,
    FrameType.CARD,
    FrameType.ERROR,
})

# async task 出站帧（hello 同类：wire 层专用，不进 StreamEvent）
ASYNC_TASK_OUTBOUND_TYPES: frozenset[str] = frozenset({
    FrameType.ASYNC_TASK_CREATED,
    FrameType.ASYNC_TASK_EVENT,
    FrameType.ASYNC_TASK_STATUS,
    FrameType.ASYNC_TASK_LIST,
    FrameType.ASYNC_TASK_SNAPSHOT,
})

# async task 入站帧（不经 SessionManager，由 RuntimeServer 直路由 AsyncTaskManager）
ASYNC_TASK_INBOUND_TYPES: frozenset[str] = frozenset({
    FrameType.ASYNC_TASK_CANCEL,
    FrameType.ASYNC_TASK_LIST_QUERY,
})


# ----------------------------------------------------------------------
# ToolResult 序列化
# ----------------------------------------------------------------------

def _decode_bytes(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def _file_to_dict(f: File) -> dict[str, Any]:
    return {"name": f.name, "mime": f.mime, "content": _decode_bytes(f.content)}


def _tool_result_to_dict(result: ToolResult) -> dict[str, Any]:
    return {
        "call_id": result.call_id,
        "status": result.status,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "artifacts": [_file_to_dict(f) for f in result.artifacts],
        "truncated": result.truncated,
        "budget_id": result.budget_id,
    }


# ----------------------------------------------------------------------
# 出站：hello + StreamEvent → 帧
# ----------------------------------------------------------------------

def _envelope(ftype: str, seq: int, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "type": ftype,
        "seq": seq,
        "ts": time.time(),
        "data": data,
    }


def hello_frame(
    session_key: str,
    model: str,
    seq: int = 0,
    *,
    providers: list[str] | None = None,
    model_provider: str | None = None,
) -> dict[str, Any]:
    """连接握手：服务端发给客户端。

    server 侧发（monoDesk adapter 发给 desktop client）。
    Runtime ↔ Gateway 这层不发 hello——Runtime 通过 session_key 注册 connection。

    Args:
        providers: 所有可用 provider 名列表（让 MonoDesk 渲染下拉框）
        model_provider: 当前 session 使用的 provider 名
    """
    data: dict[str, Any] = {"session_key": session_key, "model": model}
    if providers is not None:
        data["providers"] = providers
    if model_provider is not None:
        data["model_provider"] = model_provider
    return _envelope(FrameType.HELLO, seq, data)


def to_frame(event: StreamEvent, *, session_key: str, seq: int = 0) -> dict[str, Any] | None:
    """StreamEvent → wire frame。

    非 StreamEvent 9 类的对象返回 None，调用方负责丢弃。

    每个出站帧的 `data` 都嵌入 `session_key`——客户端用它路由事件到正确的会话
    Map entry，避免「A 的 late event 写到 B 的视图」这类跨 session 污染。
    session_key 由 RuntimeServer._outbound_consumer 在拉 per-session output_q 时
    注入（它天然持有 session_key）。
    """
    data: dict[str, Any] = {"session_key": session_key}
    if isinstance(event, StatusChange):
        data["state"] = event.state
        if event.trace_id is not None:
            data["trace_id"] = event.trace_id
        if event.turn_id is not None:
            data["turn_id"] = event.turn_id
        ftype = FrameType.STATUS
    elif isinstance(event, TokenChunk):
        data["text"] = event.text
        ftype = FrameType.TOKEN
    elif isinstance(event, ReasoningChunk):
        data["text"] = event.text
        ftype = FrameType.REASONING
    elif isinstance(event, ToolPending):
        data["call_id"] = event.call_id
        data["name"] = event.name
        data["tool_index"] = event.tool_index
        data["args_so_far"] = event.args_so_far
        ftype = FrameType.TOOL_PENDING
    elif isinstance(event, ToolStart):
        data["name"] = event.name
        data["args"] = event.args
        # call_id 让前端把 pending → start 配对成同一个块，避免双 tool card
        if event.call_id:
            data["call_id"] = event.call_id
        ftype = FrameType.TOOL_START
    elif isinstance(event, ToolEnd):
        data["name"] = event.name
        data["latency_ms"] = event.latency_ms
        data["result"] = _tool_result_to_dict(event.result)
        ftype = FrameType.TOOL_END
    elif isinstance(event, MetricChunk):
        data["metrics"] = event.metrics
        if event.trace_id is not None:
            data["trace_id"] = event.trace_id
        if event.turn_id is not None:
            data["turn_id"] = event.turn_id
        if event.model is not None:
            data["model"] = event.model
        ftype = FrameType.METRIC
    elif isinstance(event, FinalMessage):
        data["text"] = event.text
        data["metrics"] = event.metrics
        if event.trace_id is not None:
            data["trace_id"] = event.trace_id
        ftype = FrameType.FINAL
    elif isinstance(event, Card):
        data["data"] = event.data
        ftype = FrameType.CARD
    elif isinstance(event, ErrorEvent):
        data["code"] = event.code
        data["msg"] = event.msg
        data["retryable"] = event.retryable
        ftype = FrameType.ERROR
    else:
        return None
    return _envelope(ftype, seq, data)


# ----------------------------------------------------------------------
# 出站：async_task_* 帧（wire 层专用，不进 StreamEvent）
# ----------------------------------------------------------------------

def async_task_created_frame(
    *,
    session_key: str,
    task_id: str,
    kind: str,
    description: str,
    meta: dict[str, Any],
    parent_session_key: str,
    timeout_sec: float,
    created_at: float,
    seq: int = 0,
) -> dict[str, Any]:
    """fork 成功后立即发，让 MonoDesk Tasks 面板出现新行。session_key = parent_sk。"""
    return _envelope(FrameType.ASYNC_TASK_CREATED, seq, {
        "session_key": session_key,
        "task_id": task_id,
        "kind": kind,
        "description": description,
        "meta": meta,
        "parent_session_key": parent_session_key,
        "timeout_sec": timeout_sec,
        "created_at": created_at,
    })


def async_task_event_frame(
    *,
    session_key: str,
    task_id: str,
    event: StreamEvent,
    seq: int = 0,
) -> dict[str, Any] | None:
    """child SessionLoop 的 StreamEvent 转发。

    内嵌完整 StreamEvent payload：{"type": <inner type>, "data": {...}}——复用
    to_frame 的序列化，客户端用 frame_to_stream_event 同源逻辑反序列化内层。
    """
    inner = to_frame(event, session_key=session_key)
    if inner is None:
        return None
    return _envelope(FrameType.ASYNC_TASK_EVENT, seq, {
        "session_key": session_key,
        "task_id": task_id,
        "event": {"type": inner["type"], "data": inner["data"]},
    })


def async_task_status_frame(
    *,
    session_key: str,
    task_id: str,
    status: str,
    finished_at: float,
    duration_sec: float,
    final_text: str | None = None,
    error: str | None = None,
    cancel_reason: str | None = None,
    seq: int = 0,
) -> dict[str, Any]:
    """状态迁移（终态）。session_key = parent_sk。"""
    return _envelope(FrameType.ASYNC_TASK_STATUS, seq, {
        "session_key": session_key,
        "task_id": task_id,
        "status": status,
        "finished_at": finished_at,
        "duration_sec": duration_sec,
        "final_text": final_text,
        "error": error,
        "cancel_reason": cancel_reason,
    })


def async_task_list_frame(
    *, session_key: str, tasks: list[dict[str, Any]], seq: int = 0
) -> dict[str, Any]:
    """列表查询响应。tasks 是 AsyncTaskSummary dict 列表。"""
    return _envelope(FrameType.ASYNC_TASK_LIST, seq, {
        "session_key": session_key,
        "tasks": tasks,
    })


def async_task_snapshot_frame(
    *,
    session_key: str,
    task: dict[str, Any],
    recent_events: list[dict[str, Any]],
    seq: int = 0,
) -> dict[str, Any]:
    """详情查询响应：task 摘要 + 最近事件。"""
    return _envelope(FrameType.ASYNC_TASK_SNAPSHOT, seq, {
        "session_key": session_key,
        "task": task,
        "recent_events": recent_events,
    })


# ----------------------------------------------------------------------
# 入站：wire frame → InboundEvent / StreamEvent / async_task 载荷
# ----------------------------------------------------------------------

def async_task_inbound_from_frame(payload: Any) -> tuple[str, dict[str, Any]] | None:
    """解码 async_task_cancel / async_task_list_query → (type, data)。

    这两类 inbound 不指向任何 session，不走 InboundEvent / SessionManager——
    RuntimeServer 直路由给 AsyncTaskManager。其他 type 返回 None。
    """
    if not isinstance(payload, dict):
        return None
    mtype = payload.get("type")
    if mtype not in ASYNC_TASK_INBOUND_TYPES:
        return None
    data = payload.get("data")
    return (mtype, data if isinstance(data, dict) else {})


# ----------------------------------------------------------------------
# 入站：wire frame → InboundEvent（user_input / command / interrupt）
# ----------------------------------------------------------------------

def from_frame(
    payload: Any,
    *,
    default_session_key: str,
    default_source: str,
) -> InboundEvent | None:
    """解码入站帧（user_input / command / interrupt）→ InboundEvent。

    session_key 规则：
    - user_input: data.session_key 优先，缺失/空字符串则用 default_session_key
    - command / interrupt: 强制 default_session_key（防 client 跨 session 误触）

    任何解析失败（坏 JSON / 非 dict / 未知 type / 缺字段）→ 返回 None。
    """
    if not isinstance(payload, dict):
        return None
    mtype = payload.get("type")
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    ts_raw = data.get("ts")
    try:
        ts = float(ts_raw) if ts_raw is not None else time.time()
    except (TypeError, ValueError):
        ts = time.time()

    if mtype == FrameType.USER_INPUT:
        sk = data.get("session_key")
        if not isinstance(sk, str) or not sk:
            sk = default_session_key

        # attachments: list of {url, name?, mime?} → tuple[File, ...]
        # MonoDesk 上传文件到本地路径后，通过 ws 帧发送 url 过来。
        raw_attachments: list[dict[str, Any]] = data.get("attachments") or []
        attachments: list[File] = []
        for a in raw_attachments:
            if not isinstance(a, dict):
                continue
            url = a.get("url")
            if not isinstance(url, str) or not url:
                continue
            attachments.append(File(
                name=a.get("name") or url,
                content=url.encode("utf-8"),
                mime=a.get("mime", "image/png"),
            ))

        return InboundEvent(
            session_key=sk,
            kind="message",
            text=data.get("text", "") or "",
            source=default_source,
            event_type="user-input",
            timestamp=ts,
            attachments=tuple(attachments),
            meta=data.get("meta") or {},
        )
    if mtype == FrameType.COMMAND:
        return InboundEvent(
            session_key=default_session_key,
            kind="command",
            text=data.get("text", "") or "",
            source=default_source,
            event_type="command",
            timestamp=ts,
        )
    if mtype == FrameType.INTERRUPT:
        return InboundEvent(
            session_key=default_session_key,
            kind="interrupt",
            text="",
            source=default_source,
            event_type="interrupt",
            timestamp=ts,
        )
    return None


def inbound_to_frame(event: InboundEvent, seq: int = 0) -> dict[str, Any] | None:
    """把 channel 上行的 InboundEvent 编码为 user_input 帧（Gateway → Runtime）。

    - kind == "message" → user_input（保留 session_key / meta）
    - kind == "command" → command
    - kind == "interrupt" → interrupt
    - 其他 → None
    """
    if event.kind == "message":
        # attachments: File.content 存 url 字节，encode 成字符串透传
        attachments_data: list[dict[str, Any]] = []
        for f in event.attachments:
            url = f.content.decode("utf-8") if f.content else ""
            attachments_data.append({"url": url, "name": f.name, "mime": f.mime})
        return _envelope(
            FrameType.USER_INPUT,
            seq,
            {
                "session_key": event.session_key,
                "text": event.text,
                "attachments": attachments_data or None,
                "meta": event.meta,
                "ts": event.timestamp or time.time(),
            },
        )
    if event.kind == "command":
        return _envelope(
            FrameType.COMMAND,
            seq,
            {"text": event.text, "ts": event.timestamp or time.time()},
        )
    if event.kind == "interrupt":
        return _envelope(
            FrameType.INTERRUPT,
            seq,
            {"ts": event.timestamp or time.time()},
        )
    return None


def frame_to_stream_event(payload: Any) -> StreamEvent | None:
    """解码出站帧（10 种 OUTBOUND_TYPES）→ StreamEvent。

    解析失败 → None。
    """
    if not isinstance(payload, dict):
        return None
    mtype = payload.get("type")
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    if mtype == FrameType.STATUS:
        state = data.get("state")
        if state not in ("thinking", "tooling", "compressing", "wait_io", "idle"):
            return None
        return StatusChange(
            state=state,  # type: ignore[arg-type]
            trace_id=data.get("trace_id"),
            turn_id=data.get("turn_id"),
        )
    if mtype == FrameType.TOKEN:
        return TokenChunk(text=data.get("text", "") or "")
    if mtype == FrameType.REASONING:
        return ReasoningChunk(text=data.get("text", "") or "")
    if mtype == FrameType.TOOL_PENDING:
        return ToolPending(
            call_id=data.get("call_id", "") or "",
            name=data.get("name", "") or "",
            tool_index=int(data.get("tool_index", 0) or 0),
            args_so_far=data.get("args_so_far", "") or "",
        )
    if mtype == FrameType.TOOL_START:
        return ToolStart(
            name=data.get("name", "") or "",
            args=data.get("args") or {},
            call_id=data.get("call_id", "") or "",
        )
    if mtype == FrameType.TOOL_END:
        result_raw = data.get("result") or {}
        if not isinstance(result_raw, dict):
            return None
        artifacts_raw = result_raw.get("artifacts") or []
        artifacts = tuple(
            File(
                name=a.get("name", ""),
                content=(a.get("content") or "").encode("utf-8"),
                mime=a.get("mime", "application/octet-stream"),
            )
            for a in artifacts_raw if isinstance(a, dict)
        )
        try:
            result = ToolResult(
                call_id=result_raw.get("call_id", "") or "",
                status=result_raw.get("status", "ok"),
                stdout=result_raw.get("stdout", "") or "",
                stderr=result_raw.get("stderr", "") or "",
                exit_code=int(result_raw.get("exit_code", 0)),
                artifacts=artifacts,
                truncated=bool(result_raw.get("truncated", False)),
                budget_id=result_raw.get("budget_id"),
            )
        except (ValueError, TypeError):
            return None
        return ToolEnd(
            name=data.get("name", "") or "",
            result=result,
            latency_ms=int(data.get("latency_ms", 0) or 0),
        )
    if mtype == FrameType.METRIC:
        return MetricChunk(
            metrics=data.get("metrics") or {},
            trace_id=data.get("trace_id"),
            turn_id=data.get("turn_id"),
            model=data.get("model"),
        )
    if mtype == FrameType.FINAL:
        return FinalMessage(
            text=data.get("text", "") or "",
            metrics=data.get("metrics") or {},
            trace_id=data.get("trace_id"),
        )
    if mtype == FrameType.CARD:
        return Card(data=data.get("data") or {})
    if mtype == FrameType.ERROR:
        return ErrorEvent(
            code=data.get("code", "") or "",
            msg=data.get("msg", "") or "",
            retryable=bool(data.get("retryable", False)),
        )
    return None


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------

def encode(payload: dict[str, Any]) -> str:
    """frame dict → JSON 文本（wire 字节）。"""
    return json.dumps(payload, ensure_ascii=False, default=str)


def decode(raw: str | bytes) -> Any:
    """wire 字节 → frame dict。坏 JSON 返回 None（type 错误）。"""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None