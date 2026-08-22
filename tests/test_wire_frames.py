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
    assert len(OUTBOUND_TYPES) == 10


# ----------------------------------------------------------------------
# to_frame
# ----------------------------------------------------------------------

def test_to_frame_all_9_stream_events():
    cases = [
        (StatusChange(state="thinking"), "status", {"state": "thinking"}),
        (TokenChunk(text="hi"), "token", {"text": "hi"}),
        (ReasoningChunk(text="r"), "reasoning", {"text": "r"}),
        (ToolStart(name="bash", args={"cmd": "ls"}), "tool_start", {"name": "bash", "args": {"cmd": "ls"}}),
        (MetricChunk(metrics={"steps": 1}), "metric", {"metrics": {"steps": 1}}),
        (FinalMessage(text="done", metrics={"x": 1}), "final", {"text": "done", "metrics": {"x": 1}}),
        (Card(data={"foo": 1}), "card", {"data": {"foo": 1}}),
        (ErrorEvent(code="E_TIMEOUT", msg="slow", retryable=True),
         "error", {"code": "E_TIMEOUT", "msg": "slow", "retryable": True}),
    ]
    for ev, expected_type, expected_data in cases:
        f = to_frame(ev, seq=42)
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
    f = to_frame(ToolEnd(name="bash", result=r, latency_ms=123), seq=1)
    assert f["type"] == "tool_end"
    assert f["data"]["name"] == "bash"
    assert f["data"]["latency_ms"] == 123
    assert f["data"]["result"]["call_id"] == "c1"
    assert f["data"]["result"]["status"] == "ok"
    assert f["data"]["result"]["artifacts"] == [
        {"name": "a.txt", "mime": "text/plain", "content": "hi"}
    ]


def test_to_frame_unknown_returns_none():
    # 未知事件类型
    class Weird:
        pass
    assert to_frame(Weird(), seq=0) is None


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
    ), seq=1)
    decoded = frame_to_stream_event(f)
    assert isinstance(decoded, ToolEnd)
    assert decoded.name == "bash"
    assert decoded.latency_ms == 99
    assert decoded.result.call_id == "c1"
    assert decoded.result.stdout == "o"
    assert decoded.result.artifacts[0].name == "a"


def test_frame_to_stream_event_final():
    f = to_frame(FinalMessage(text="done", metrics={"x": 1}), seq=1)
    decoded = frame_to_stream_event(f)
    assert isinstance(decoded, FinalMessage)
    assert decoded.text == "done"
    assert decoded.metrics == {"x": 1}


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
    f = to_frame(TokenChunk(text="hello"), seq=3)
    raw = encode(f)
    assert isinstance(raw, str)
    decoded = decode(raw)
    assert decoded == f


def test_decode_bad_json_returns_none():
    assert decode("not json{") is None
    assert decode(b"\x00\x01") is None