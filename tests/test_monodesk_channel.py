"""extensions/channels/monodesk.py 单测。

测试适配新 in-process adapter 形态：
- `_to_frame` / `_hello` / `_dispatch_inbound` 私有方法已抽到 wire_frames
- send() 把 (None, frame) tuple 入 _out_q；用 send_frame_direct 透传 Runtime seq
- listen() 收帧后 yield InboundEvent
"""
from __future__ import annotations

import asyncio
import json

import pytest
from websockets.sync.client import connect as sync_connect

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
    decode,
    hello_frame,
    to_frame,
)
from extensions.channels.monodesk import MonoDeskChannel, MonoDeskChannelConfig


def _alloc_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_channel(port: int, session_key: str = "s1") -> MonoDeskChannel:
    cfg = MonoDeskChannelConfig(host="127.0.0.1", port=port, model="gpt-x")
    return MonoDeskChannel(cfg, session_key=session_key)


def _drain_out_frames(ch: MonoDeskChannel) -> list[dict]:
    """drain _out_q 里所有 frame tuple。"""
    out: list[dict] = []
    while True:
        try:
            item = ch._out_q.get_nowait()
        except Exception:
            break
        if item is None:
            break
        _, frame = item
        out.append(frame)
    return out


# ----------------------------------------------------------------------
# Frame mapping（用纯函数 to_frame 验证，不依赖 MonoDeskChannel 实例）
# ----------------------------------------------------------------------

class TestFrameMapping:
    def test_status(self):
        f = to_frame(StatusChange(state="thinking"))
        assert f["type"] == "status" and f["data"]["state"] == "thinking"

    def test_token(self):
        f = to_frame(TokenChunk(text="hi"))
        assert f["type"] == "token" and f["data"]["text"] == "hi"

    def test_reasoning(self):
        f = to_frame(ReasoningChunk(text="r"))
        assert f["type"] == "reasoning" and f["data"]["text"] == "r"

    def test_tool_start(self):
        f = to_frame(ToolStart(name="bash", args={"cmd": "ls"}))
        assert f["type"] == "tool_start"
        assert f["data"]["name"] == "bash" and f["data"]["args"] == {"cmd": "ls"}

    def test_tool_end_result(self):
        r = ToolResult(
            call_id="c1", status="ok", stdout="o", stderr="",
            exit_code=0, artifacts=(File(name="a.txt", content=b"hi", mime="text/plain"),),
        )
        f = to_frame(ToolEnd(name="bash", result=r, latency_ms=10))
        assert f["type"] == "tool_end"
        assert f["data"]["name"] == "bash"
        assert f["data"]["latency_ms"] == 10
        assert f["data"]["result"]["call_id"] == "c1"
        assert f["data"]["result"]["artifacts"][0] == {
            "name": "a.txt", "mime": "text/plain", "content": "hi"
        }

    def test_metric(self):
        f = to_frame(MetricChunk(metrics={"steps": 1}))
        assert f["type"] == "metric" and f["data"]["metrics"] == {"steps": 1}

    def test_final(self):
        f = to_frame(FinalMessage(text="done"))
        assert f["type"] == "final" and f["data"]["text"] == "done"

    def test_error(self):
        f = to_frame(ErrorEvent(code="E", msg="m", retryable=False))
        assert f["type"] == "error" and f["data"]["code"] == "E"

    def test_seq_is_monotonic(self):
        from itertools import count
        c = count()
        seqs = [to_frame(TokenChunk(text="x"), seq=next(c))["seq"] for _ in range(5)]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == 5

    def test_unknown_event_returns_none(self):
        assert to_frame("not an event") is None  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Hello
# ----------------------------------------------------------------------

class TestHello:
    def test_hello_carries_session_and_model(self):
        h = hello_frame(session_key="s1", model="gpt-4", seq=0)
        assert h["type"] == "hello"
        assert h["data"]["session_key"] == "s1"
        assert h["data"]["model"] == "gpt-4"


# ----------------------------------------------------------------------
# Inbound 映射
# ----------------------------------------------------------------------

class TestInboundMapping:
    def test_user_input(self):
        # 直接用 from_frame 验证（adapter 只是包装）
        from core.protocol.wire_frames import from_frame
        f = to_frame(TokenChunk(text="x"))  # 用任意帧模板无所谓
        f = {"type": "user_input", "data": {"text": "hi", "session_key": "s1"}}
        ev = from_frame(f, default_session_key="s1", default_source="monodesk")
        assert ev.kind == "message" and ev.text == "hi" and ev.session_key == "s1"

    def test_interrupt(self):
        from core.protocol.wire_frames import from_frame
        f = {"type": "interrupt", "data": {}}
        ev = from_frame(f, default_session_key="s1", default_source="monodesk")
        assert ev.kind == "interrupt" and ev.session_key == "s1"


# ----------------------------------------------------------------------
# Send / listen 队列桥
# ----------------------------------------------------------------------

class TestQueueBridge:
    @pytest.mark.asyncio
    async def test_send_queues_frame(self):
        ch = MonoDeskChannel(MonoDeskChannelConfig(host="127.0.0.1", port=9999))
        await ch.send(TokenChunk(text="hi"))
        frames = _drain_out_frames(ch)
        assert len(frames) == 1
        assert frames[0]["type"] == "token"
        assert frames[0]["data"]["text"] == "hi"

    @pytest.mark.asyncio
    async def test_send_unknown_drops_silently(self):
        ch = MonoDeskChannel(MonoDeskChannelConfig(host="127.0.0.1", port=9999))

        class Weird:
            pass

        await ch.send(Weird())  # type: ignore[arg-type]
        assert _drain_out_frames(ch) == []

    @pytest.mark.asyncio
    async def test_listen_drains_inbound(self):
        ch = MonoDeskChannel(MonoDeskChannelConfig(host="127.0.0.1", port=9999))
        # 模拟 recv 线程 dispatch
        ch._dispatch_inbound(json.dumps({
            "type": "user_input", "data": {"text": "hi", "session_key": "s1"},
        }))
        ch._wake_for_test()
        events: list[InboundEvent] = []
        async for ev in ch.listen():
            events.append(ev)
            if len(events) >= 1:
                break
        assert len(events) == 1
        assert events[0].kind == "message" and events[0].text == "hi"

    @pytest.mark.asyncio
    async def test_listen_stops_on_stop(self):
        ch = MonoDeskChannel(MonoDeskChannelConfig(host="127.0.0.1", port=9999))
        # 不调 start，只测 stop 后 listen 立刻退出
        # listen 启动时先 check _stop；stop 已设 _stop → 直接退出
        await ch.stop()
        events = []
        async for ev in ch.listen():
            events.append(ev)
        assert events == []

    def test_interrupt_uses_adapter_session_key(self):
        ch = _make_channel(9999, session_key="MY_SESSION")
        ch._dispatch_inbound(json.dumps({
            "type": "interrupt", "data": {"session_key": "HACK"},
        }))
        ev = ch._in_q.get_nowait()
        assert ev.session_key == "MY_SESSION"

    def test_command_uses_adapter_session_key(self):
        ch = _make_channel(9999, session_key="MY_SESSION")
        ch._dispatch_inbound(json.dumps({
            "type": "command", "data": {"text": "/x", "session_key": "HACK"},
        }))
        ev = ch._in_q.get_nowait()
        assert ev.kind == "command" and ev.session_key == "MY_SESSION"

    def test_unknown_frame_dropped(self):
        ch = _make_channel(9999)
        ch._dispatch_inbound(json.dumps({"type": "wat"}))
        assert ch._in_q.qsize() == 0


# ----------------------------------------------------------------------
# 端到端：mock desktop client ↔ in-process MonoDeskChannel
# ----------------------------------------------------------------------

class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_ws_roundtrip_in_process(self):
        """起 MonoDeskChannel（adapter 模式）+ mock desktop client 连过来。
        端到端验证 hello / send / listen 三向 roundtrip。
        """
        port = _alloc_port()
        ch = _make_channel(port, session_key="S")
        await ch.start()
        try:
            import threading
            captured: list[dict] = []
            errors: list[Exception] = []

            def client_thread():
                try:
                    with sync_connect(f"ws://127.0.0.1:{port}", open_timeout=2) as ws:
                        # 1) hello
                        captured.append(decode(ws.recv(timeout=2)))
                        # 2) 收 token
                        captured.append(decode(ws.recv(timeout=2)))
                except Exception as e:
                    errors.append(e)

            t = threading.Thread(target=client_thread, daemon=True)
            t.start()
            await asyncio.sleep(0.2)  # 等 client 连上 + 收 hello

            # adapter send token
            await ch.send(TokenChunk(text="hi"))
            await asyncio.sleep(0.3)

            # 1) 发 user_input
            with sync_connect(f"ws://127.0.0.1:{port}", open_timeout=2) as ws:
                ws.recv(timeout=2)  # 自己的 hello
                ws.send(json.dumps({
                    "type": "user_input", "data": {"text": "hello", "session_key": "S"},
                }))
                # 等 adapter 处理 → listen() 拿到
                events = []
                async for ev in ch.listen():
                    events.append(ev)
                    if len(events) >= 1:
                        break

            assert captured[0]["type"] == "hello"
            assert captured[0]["data"]["session_key"] == "S"
            assert captured[1]["type"] == "token"
            assert captured[1]["data"]["text"] == "hi"
            assert events[0].kind == "message" and events[0].text == "hello"
            t.join(timeout=2)
            assert not errors, errors
        finally:
            await ch.stop()

    @pytest.mark.asyncio
    async def test_send_frame_direct_uses_runtime_seq(self):
        """Gateway 用 send_frame_direct 透传 Runtime seq：桌面客户端看到 Runtime 单调 seq。"""
        port = _alloc_port()
        ch = _make_channel(port)
        await ch.start()
        try:
            import threading
            captured: list[dict] = []
            errors: list[Exception] = []

            def client_thread():
                try:
                    with sync_connect(f"ws://127.0.0.1:{port}", open_timeout=2) as ws:
                        ws.recv(timeout=2)  # hello
                        for _ in range(2):
                            captured.append(decode(ws.recv(timeout=2)))
                except Exception as e:
                    errors.append(e)

            t = threading.Thread(target=client_thread, daemon=True)
            t.start()
            await asyncio.sleep(0.2)

            # 用 send_frame_direct 模拟 Gateway 透传 Runtime seq=42 / 43
            ch.send_frame_direct(42, to_frame(TokenChunk(text="x"), seq=42))
            ch.send_frame_direct(43, to_frame(TokenChunk(text="y"), seq=43))
            await asyncio.sleep(0.3)
            assert len(captured) == 2
            assert captured[0]["seq"] == 42
            assert captured[1]["seq"] == 43
            t.join(timeout=2)
            assert not errors, errors
        finally:
            await ch.stop()