"""core/protocol/wire_frames.py 单测。"""
from __future__ import annotations

import json

import pytest

from core.protocol import (
    Card,
    ErrorEvent,
    File,
    FinalMessage,
    InboundEvent,
    MetricChunk,
    ReasoningChunk,
    StatusChange,
    TokenChunk,
    ToolEnd,
    ToolResult,
    ToolStart,
)
from core.protocol.wire_frames import (
    INBOUND_TYPES,
    OUTBOUND_TYPES,
    PROTOCOL_VERSION,
    decode,
    encode,
    frame_to_stream_event,
    from_frame,
    hello_frame,
    inbound_to_frame,
    to_frame,
)


# ----------------------------------------------------------------------
# FrameType / 版本号
# ----------------------------------------------------------------------

def test_protocol_version_is_1():
    assert PROTOCOL_VERSION == 1


def test_inbound_outbound_types_disjoint():
    assert INBOUND_TYPES.isdisjoint(OUTBOUND_TYPES)
    assert len(INBOUND_TYPES) == 3
    assert len(OUTBOUND_TYPES) == 11


# ----------------------------------------------------------------------
# to_frame
# ----------------------------------------------------------------------

def test_to_frame_all_9_stream_events():
    cases = [
        (StatusChange(state="thinking"), "status", {"session_key": "s", "state": "thinking"}),
        (TokenChunk(text="hi"), "token", {"session_key": "s", "text": "hi"}),
        (ReasoningChunk(text="r"), "reasoning", {"session_key": "s", "text": "r"}),
        (ToolStart(name="bash", args={"cmd": "ls"}), "tool_start",
         {"session_key": "s", "name": "bash", "args": {"cmd": "ls"}}),
        (MetricChunk(metrics={"steps": 1}), "metric",
         {"session_key": "s", "metrics": {"steps": 1}}),
        (FinalMessage(text="done", metrics={"x": 1}), "final",
         {"session_key": "s", "text": "done", "metrics": {"x": 1}}),
        (Card(data={"foo": 1}), "card", {"session_key": "s", "data": {"foo": 1}}),
        (ErrorEvent(code="E_TIMEOUT", msg="slow", retryable=True),
         "error", {"session_key": "s", "code": "E_TIMEOUT", "msg": "slow", "retryable": True}),
    ]
    for ev, expected_type, expected_data in cases:
        f = to_frame(ev, session_key="s", seq=42)
        assert f["v"] == PROTOCOL_VERSION
        assert f["type"] == expected_type
        assert f["seq"] == 42
        assert f["data"] == expected_data
        assert "ts" in f


def test_to_frame_tool_end_includes_result_dict():
    r = ToolResult(
        call_id="c1", status="ok", stdout="o", stderr="",
        exit_code=0, artifacts=(File(name="a.txt", content=b"hi", mime="text/plain"),),
    )
    f = to_frame(ToolEnd(name="bash", result=r, latency_ms=123), session_key="s", seq=1)
    assert f["type"] == "tool_end"
    assert f["data"]["session_key"] == "s"
    assert f["data"]["name"] == "bash"
    assert f["data"]["latency_ms"] == 123
    assert f["data"]["result"]["call_id"] == "c1"
    assert f["data"]["result"]["status"] == "ok"
    assert f["data"]["result"]["artifacts"] == [
        {"name": "a.txt", "mime": "text/plain", "content": "hi"}
    ]


def test_to_frame_session_key_always_present():
    """回归测试：每个出站 frame 必须带 session_key —— 这是 MonoDesk 路由的依赖。
    如果 to_frame 漏掉某个分支忘了加 session_key，客户端会拿不到路由信息
    退回到 streamKeyRef hack，于是跨 session 事件污染就回来了。"""
    events = [
        StatusChange(state="thinking"),
        TokenChunk(text="x"),
        ReasoningChunk(text="y"),
        ToolStart(name="bash", args={"cmd": "ls"}),
        ToolEnd(name="bash", result=ToolResult(call_id="c", status="ok", stdout="", stderr="", exit_code=0), latency_ms=10),
        MetricChunk(metrics={"x": 1}),
        FinalMessage(text="done", metrics={}),
        Card(data={"foo": 1}),
        ErrorEvent(code="E", msg="m", retryable=False),
    ]
    for ev in events:
        f = to_frame(ev, session_key="test-sk")
        assert f is not None, f"to_frame returned None for {type(ev).__name__}"
        assert "session_key" in f["data"], f"missing session_key for {type(ev).__name__}"
        assert f["data"]["session_key"] == "test-sk"


def test_to_frame_unknown_returns_none():
    # 未知事件类型
    class Weird:
        pass
    assert to_frame(Weird(), session_key="s", seq=0) is None


def test_to_frame_requires_session_key_kwarg():
    # 防止有人误用 positional session_key —— 它必须是 keyword argument，
    # 避免和未来的 seq 参数位置冲突。
    import inspect
    sig = inspect.signature(to_frame)
    params = list(sig.parameters.values())
    assert params[1].name == "session_key"
    assert params[1].kind == inspect.Parameter.KEYWORD_ONLY


# ----------------------------------------------------------------------
# from_frame
# ----------------------------------------------------------------------

def test_from_frame_user_input_overrides_session_key():
    f = {"type": "user_input", "data": {"text": "hi", "session_key": "s1", "meta": {"k": 1}}}
    ev = from_frame(f, default_session_key="default", default_source="monodesk")
    assert ev is not None
    assert ev.kind == "message"
    assert ev.text == "hi"
    assert ev.session_key == "s1"
    assert ev.source == "monodesk"
    assert ev.meta == {"k": 1}


def test_from_frame_user_input_falls_back_to_default_session_key():
    f = {"type": "user_input", "data": {"text": "hi"}}
    ev = from_frame(f, default_session_key="fallback", default_source="monodesk")
    assert ev is not None
    assert ev.session_key == "fallback"


def test_from_frame_user_input_empty_session_key_falls_back():
    f = {"type": "user_input", "data": {"text": "hi", "session_key": ""}}
    ev = from_frame(f, default_session_key="fb", default_source="x")
    assert ev is not None
    assert ev.session_key == "fb"


def test_from_frame_command_uses_default_session_key():
    f = {"type": "command", "data": {"text": "do", "session_key": "BAD"}}
    ev = from_frame(f, default_session_key="real", default_source="x")
    assert ev is not None
    assert ev.kind == "command"
    assert ev.text == "do"
    assert ev.session_key == "real"  # BAD 被忽略


def test_from_frame_interrupt_uses_default_session_key():
    f = {"type": "interrupt", "data": {"session_key": "BAD"}}
    ev = from_frame(f, default_session_key="real", default_source="x")
    assert ev is not None
    assert ev.kind == "interrupt"
    assert ev.text == ""
    assert ev.session_key == "real"


def test_from_frame_unknown_type_returns_none():
    assert from_frame({"type": "wat"}, default_session_key="d", default_source="x") is None


def test_from_frame_non_dict_returns_none():
    assert from_frame("not a dict", default_session_key="d", default_source="x") is None
    assert from_frame(None, default_session_key="d", default_source="x") is None


def test_from_frame_uses_default_source():
    f = {"type": "user_input", "data": {"text": "x"}}
    ev = from_frame(f, default_session_key="s", default_source="custom-src")
    assert ev is not None and ev.source == "custom-src"


# ----------------------------------------------------------------------
# hello_frame
# ----------------------------------------------------------------------

def test_hello_frame_schema():
    f = hello_frame(session_key="s1", model="gpt-4", seq=7)
    assert f["v"] == PROTOCOL_VERSION
    assert f["type"] == "hello"
    assert f["seq"] == 7
    assert f["data"] == {"session_key": "s1", "model": "gpt-4"}


# ----------------------------------------------------------------------
# inbound_to_frame（Gateway → Runtime 上行编码）
# ----------------------------------------------------------------------

def test_inbound_to_frame_message_includes_session_and_meta():
    ev = InboundEvent(
        session_key="s1", kind="message", text="hi",
        source="terminal", event_type="user-input",
        timestamp=123.4, meta={"k": "v"},
    )
    f = inbound_to_frame(ev, seq=5)
    assert f is not None
    assert f["type"] == "user_input"
    assert f["seq"] == 5
    assert f["data"]["session_key"] == "s1"
    assert f["data"]["text"] == "hi"
    assert f["data"]["meta"] == {"k": "v"}


def test_inbound_to_frame_interrupt():
    ev = InboundEvent(session_key="s1", kind="interrupt", text="", source="x")
    f = inbound_to_frame(ev, seq=1)
    assert f is not None
    assert f["type"] == "interrupt"


def test_inbound_to_frame_command():
    ev = InboundEvent(session_key="s1", kind="command", text="/reset", source="x")
    f = inbound_to_frame(ev, seq=1)
    assert f is not None
    assert f["type"] == "command"
    assert f["data"]["text"] == "/reset"


def test_inbound_to_frame_unknown_kind_returns_none():
    ev = InboundEvent(session_key="s1", kind="attachment", text="", source="x")
    assert inbound_to_frame(ev, seq=0) is None


# ----------------------------------------------------------------------
# frame_to_stream_event（Runtime → Gateway 下行解码）
# ----------------------------------------------------------------------

def test_frame_to_stream_event_token():
    f = {"type": "token", "data": {"text": "abc"}}
    ev = frame_to_stream_event(f)
    assert isinstance(ev, TokenChunk) and ev.text == "abc"


def test_frame_to_stream_event_status_validates_state():
    assert isinstance(frame_to_stream_event({"type": "status", "data": {"state": "thinking"}}), StatusChange)
    assert frame_to_stream_event({"type": "status", "data": {"state": "BOGUS"}}) is None


def test_frame_to_stream_event_tool_end_round_trip():
    f = to_frame(ToolEnd(
        name="bash",
        result=ToolResult(call_id="c1", status="ok", stdout="o", stderr="",
                          exit_code=0, artifacts=(File(name="a", content=b"x"),)),
        latency_ms=99,
    ), session_key="s", seq=1)
    decoded = frame_to_stream_event(f)
    assert isinstance(decoded, ToolEnd)
    assert decoded.name == "bash"
    assert decoded.latency_ms == 99
    assert decoded.result.call_id == "c1"
    assert decoded.result.stdout == "o"
    assert decoded.result.artifacts[0].name == "a"


def test_frame_to_stream_event_final():
    f = to_frame(FinalMessage(text="done", metrics={"x": 1}), session_key="s", seq=1)
    decoded = frame_to_stream_event(f)
    assert isinstance(decoded, FinalMessage)
    assert decoded.text == "done"
    assert decoded.metrics == {"x": 1}


def test_trace_id_round_trips_on_status_metric_final():
    """可观测性：StatusChange / MetricChunk / FinalMessage 的 trace_id / turn_id
    序列化 + 反序列化保持一致；None / 缺字段都不破坏旧 client 解码。"""
    # Status
    f = to_frame(StatusChange(state="thinking", trace_id="t_1", turn_id="u_1"),
                 session_key="s")
    assert f["data"]["trace_id"] == "t_1"
    assert f["data"]["turn_id"] == "u_1"
    assert f["data"]["session_key"] == "s"
    dec = frame_to_stream_event(f)
    assert isinstance(dec, StatusChange)
    assert dec.trace_id == "t_1"
    assert dec.turn_id == "u_1"
    # 不带 trace_id 也工作（旧 client）
    f = to_frame(StatusChange(state="thinking"), session_key="s")
    assert "trace_id" not in f["data"]
    assert f["data"]["session_key"] == "s"
    dec = frame_to_stream_event(f)
    assert isinstance(dec, StatusChange)
    assert dec.trace_id is None
    # Metric
    f = to_frame(MetricChunk(metrics={"x": 1}, trace_id="t_1", turn_id="u_1"),
                 session_key="s")
    assert f["data"]["trace_id"] == "t_1"
    dec = frame_to_stream_event(f)
    assert isinstance(dec, MetricChunk)
    assert dec.trace_id == "t_1" and dec.turn_id == "u_1"
    # Final
    f = to_frame(FinalMessage(text="done", metrics={}, trace_id="t_1"),
                 session_key="s")
    assert f["data"]["trace_id"] == "t_1"
    dec = frame_to_stream_event(f)
    assert isinstance(dec, FinalMessage)
    assert dec.trace_id == "t_1"


def test_legacy_client_ignores_trace_id_field():
    """旧客户端只读 state / metrics / text；frame 里多 trace_id / turn_id 字段不应报错。"""
    f = to_frame(StatusChange(state="thinking", trace_id="t_1", turn_id="u_1"),
                 session_key="s")
    # 模拟旧 client：只读 state
    assert f["type"] == "status"
    assert f["data"]["state"] == "thinking"
    # trace_id / turn_id 是 additive 字段，旧 client 解码忽略无害


def test_legacy_frame_without_trace_id_decodes_with_none():
    """MonoX 老版本发的 frame 没 trace_id 字段；新 client 解码 trace_id=None。"""
    f = {
        "v": 1, "type": "status", "seq": 0, "ts": 0,
        "data": {"state": "thinking"},
    }
    dec = frame_to_stream_event(f)
    assert isinstance(dec, StatusChange)
    assert dec.trace_id is None and dec.turn_id is None


def test_frame_to_stream_event_card_and_error():
    assert isinstance(frame_to_stream_event({"type": "card", "data": {"data": {"k": 1}}}),
                      Card)
    err = frame_to_stream_event({"type": "error", "data": {"code": "E", "msg": "m", "retryable": False}})
    assert isinstance(err, ErrorEvent) and err.code == "E" and err.retryable is False


def test_frame_to_stream_event_unknown_returns_none():
    assert frame_to_stream_event({"type": "wat"}) is None
    assert frame_to_stream_event(None) is None


# ----------------------------------------------------------------------
# encode / decode
# ----------------------------------------------------------------------

def test_encode_decode_round_trip():
    f = to_frame(TokenChunk(text="hello"), session_key="s", seq=3)
    raw = encode(f)
    assert isinstance(raw, str)
    decoded = decode(raw)
    assert decoded == f


def test_decode_bad_json_returns_none():
    assert decode("not json{") is None
    assert decode(b"\x00\x01") is None

# ----------------------------------------------------------------------
# async_task_* 帧（wire 层专用，见 requirements/async-task.md）
# ----------------------------------------------------------------------

from core.protocol.wire_frames import (  # noqa: E402
    ASYNC_TASK_INBOUND_TYPES,
    ASYNC_TASK_OUTBOUND_TYPES,
    FrameType,
    async_task_created_frame,
    async_task_event_frame,
    async_task_inbound_from_frame,
    async_task_list_frame,
    async_task_snapshot_frame,
    async_task_status_frame,
)


def test_async_task_frame_type_constants():
    assert FrameType.ASYNC_TASK_CREATED == "async_task_created"
    assert FrameType.ASYNC_TASK_EVENT == "async_task_event"
    assert FrameType.ASYNC_TASK_STATUS == "async_task_status"
    assert FrameType.ASYNC_TASK_LIST == "async_task_list"
    assert FrameType.ASYNC_TASK_SNAPSHOT == "async_task_snapshot"
    assert FrameType.ASYNC_TASK_CANCEL == "async_task_cancel"
    assert FrameType.ASYNC_TASK_LIST_QUERY == "async_task_list_query"
    assert len(ASYNC_TASK_OUTBOUND_TYPES) == 5
    assert len(ASYNC_TASK_INBOUND_TYPES) == 2
    # 与既有集合不相交
    assert ASYNC_TASK_OUTBOUND_TYPES | ASYNC_TASK_INBOUND_TYPES \
        and not (ASYNC_TASK_OUTBOUND_TYPES & ASYNC_TASK_INBOUND_TYPES)


def test_async_task_created_frame_fields():
    f = async_task_created_frame(
        session_key="default",
        task_id="t_4f9ea1b2c3d4",
        kind="subagent",
        description="review PR",
        meta={"priority": "high"},
        parent_session_key="default",
        timeout_sec=1800.0,
        created_at=1732000000.0,
        seq=7,
    )
    assert f["v"] == PROTOCOL_VERSION and f["type"] == "async_task_created" and f["seq"] == 7
    d = f["data"]
    assert d["task_id"] == "t_4f9ea1b2c3d4" and d["kind"] == "subagent"
    assert d["parent_session_key"] == "default" and d["timeout_sec"] == 1800.0


def test_async_task_event_frame_nests_stream_event():
    f = async_task_event_frame(
        session_key="default", task_id="t_x", event=TokenChunk(text="hi"), seq=1
    )
    assert f["type"] == "async_task_event"
    inner = f["data"]["event"]
    assert inner["type"] == "token" and inner["data"]["text"] == "hi"
    assert f["data"]["session_key"] == "default" and f["data"]["task_id"] == "t_x"
    # 内层可被 frame_to_stream_event 同源逻辑还原
    restored = frame_to_stream_event({"type": inner["type"], "data": inner["data"]})
    assert isinstance(restored, TokenChunk) and restored.text == "hi"


def test_async_task_status_frame_nullables():
    f = async_task_status_frame(
        session_key="p", task_id="t_x", status="cancelled",
        finished_at=1732000100.0, duration_sec=100.0, cancel_reason="timeout", seq=2,
    )
    d = f["data"]
    assert d["status"] == "cancelled" and d["cancel_reason"] == "timeout"
    assert d["final_text"] is None and d["error"] is None


def test_async_task_list_and_snapshot_frames():
    lf = async_task_list_frame(session_key="default", tasks=[{"task_id": "t_x", "status": "running"}])
    assert lf["type"] == "async_task_list" and lf["data"]["tasks"][0]["task_id"] == "t_x"
    sf = async_task_snapshot_frame(
        session_key="default", task={"task_id": "t_x"}, recent_events=[{"kind": "status"}]
    )
    assert sf["type"] == "async_task_snapshot"
    assert sf["data"]["recent_events"] == [{"kind": "status"}]


def test_async_task_inbound_from_frame():
    at = async_task_inbound_from_frame({
        "v": 1, "type": "async_task_cancel",
        "data": {"task_id": "t_x", "reason": "user"},
    })
    assert at == ("async_task_cancel", {"task_id": "t_x", "reason": "user"})
    at2 = async_task_inbound_from_frame({
        "v": 1, "type": "async_task_list_query", "data": {"session_key": "default"},
    })
    assert at2 is not None and at2[0] == "async_task_list_query"
    # 非 async 帧返回 None
    assert async_task_inbound_from_frame({"type": "user_input", "data": {"text": "hi"}}) is None
    assert async_task_inbound_from_frame({"type": "async_task_cancel"}) == \
        ("async_task_cancel", {})
    assert async_task_inbound_from_frame("not a dict") is None
    assert async_task_inbound_from_frame(None) is None
