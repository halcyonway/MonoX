"""MonoDeskChannel 单元测试：协议映射 + WebSocket 端到端往返。"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.protocol import (
    ErrorEvent,
    FinalMessage,
    MetricChunk,
    ReasoningChunk,
    StatusChange,
    TokenChunk,
    ToolEnd,
    ToolResult,
    ToolStart,
)
from extensions.channels.monodesk import MonoDeskChannel, MonoDeskChannelConfig


@pytest.fixture
def cfg() -> MonoDeskChannelConfig:
    return MonoDeskChannelConfig(host="127.0.0.1", port=0, model="gpt-4")


@pytest.fixture
def ch(cfg: MonoDeskChannelConfig) -> MonoDeskChannel:
    return MonoDeskChannel(cfg, session_key="test")


# ------------------------------------------------------------------
# 出站映射：StreamEvent → 帧
# ------------------------------------------------------------------

class TestFrameMapping:
    def test_status(self, ch: MonoDeskChannel):
        f = ch._to_frame(StatusChange(state="thinking"))
        assert f["v"] == 1
        assert f["type"] == "status"
        assert f["data"] == {"state": "thinking"}
        assert "seq" in f and "ts" in f

    def test_token(self, ch: MonoDeskChannel):
        f = ch._to_frame(TokenChunk(text="你好"))
        assert f["type"] == "token"
        assert f["data"] == {"text": "你好"}

    def test_reasoning(self, ch: MonoDeskChannel):
        f = ch._to_frame(ReasoningChunk(text="让我想想"))
        assert f["type"] == "reasoning"
        assert f["data"] == {"text": "让我想想"}

    def test_tool_start(self, ch: MonoDeskChannel):
        f = ch._to_frame(ToolStart(name="bash", args={"command": "ls"}))
        assert f["type"] == "tool_start"
        assert f["data"] == {"name": "bash", "args": {"command": "ls"}}

    def test_tool_end_result(self, ch: MonoDeskChannel):
        r = ToolResult(call_id="call_1", status="ok", stdout="a\nb\n", stderr="", exit_code=0)
        f = ch._to_frame(ToolEnd(name="bash", result=r, latency_ms=214))
        assert f["type"] == "tool_end"
        assert f["data"]["latency_ms"] == 214
        assert f["data"]["result"]["call_id"] == "call_1"
        assert f["data"]["result"]["status"] == "ok"
        assert f["data"]["result"]["exit_code"] == 0
        assert f["data"]["result"]["truncated"] is False

    def test_metric(self, ch: MonoDeskChannel):
        f = ch._to_frame(MetricChunk(metrics={"step_idx": 1, "latency_ms": 812}))
        assert f["type"] == "metric"
        assert f["data"]["metrics"] == {"step_idx": 1, "latency_ms": 812}

    def test_final(self, ch: MonoDeskChannel):
        f = ch._to_frame(FinalMessage(text="done", metrics={"steps": 2}))
        assert f["type"] == "final"
        assert f["data"]["text"] == "done"
        assert f["data"]["metrics"] == {"steps": 2}

    def test_error(self, ch: MonoDeskChannel):
        f = ch._to_frame(ErrorEvent(code="llm_timeout", msg="t", retryable=True))
        assert f["type"] == "error"
        assert f["data"] == {"code": "llm_timeout", "msg": "t", "retryable": True}

    def test_seq_is_monotonic(self, ch: MonoDeskChannel):
        seqs = [ch._to_frame(TokenChunk(text="x"))["seq"] for _ in range(5)]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == 5

    def test_unknown_event_returns_none(self, ch: MonoDeskChannel):
        # 不属于 9 类 union 的对象 → None（不广播）
        assert ch._to_frame("not an event") is None  # type: ignore[arg-type]


# ------------------------------------------------------------------
# hello 帧
# ------------------------------------------------------------------

class TestHello:
    def test_hello_carries_session_and_model(self, ch: MonoDeskChannel):
        h = ch._hello()
        assert h["type"] == "hello"
        assert h["data"]["session_key"] == "test"
        assert h["data"]["model"] == "gpt-4"


# ------------------------------------------------------------------
# 入站映射：帧 → InboundEvent
# ------------------------------------------------------------------

class TestInbound:
    def test_user_input(self, ch: MonoDeskChannel):
        ch._dispatch_inbound(json.dumps({"type": "user_input", "data": {"text": "hi"}}))
        ev = ch._in_q.get_nowait()
        assert ev.kind == "message"
        assert ev.source == "monodesk"
        assert ev.event_type == "user-input"
        assert ev.text == "hi"
        assert ev.session_key == "test"

    def test_command(self, ch: MonoDeskChannel):
        ch._dispatch_inbound(json.dumps({"type": "command", "data": {"text": "/debug on"}}))
        ev = ch._in_q.get_nowait()
        assert ev.kind == "command"
        assert ev.event_type == "command"

    def test_interrupt(self, ch: MonoDeskChannel):
        ch._dispatch_inbound(json.dumps({"type": "interrupt", "data": {}}))
        ev = ch._in_q.get_nowait()
        assert ev.kind == "interrupt"

    def test_bad_json_ignored(self, ch: MonoDeskChannel):
        ch._dispatch_inbound("not json")
        assert ch._in_q.empty()

    def test_unknown_type_ignored(self, ch: MonoDeskChannel):
        ch._dispatch_inbound(json.dumps({"type": "nope", "data": {}}))
        assert ch._in_q.empty()


# ------------------------------------------------------------------
# send / listen 队列桥接
# ------------------------------------------------------------------

class TestQueueBridge:
    async def test_send_queues_frame(self, ch: MonoDeskChannel):
        await ch.send(TokenChunk(text="hello"))
        f = ch._out_q.get_nowait()
        assert f["type"] == "token"
        assert f["data"]["text"] == "hello"

    async def test_listen_yields_inbound(self, ch: MonoDeskChannel):
        ch._dispatch_inbound(json.dumps({"type": "user_input", "data": {"text": "hi"}}))
        async for ev in ch.listen():
            assert ev.text == "hi"
            ch._stop.set()


# ------------------------------------------------------------------
# WebSocket 端到端
# ------------------------------------------------------------------

class TestEndToEnd:
    def test_ws_roundtrip(self, cfg: MonoDeskChannelConfig):
        from websockets.sync.client import connect

        ch = MonoDeskChannel(cfg, session_key="test")
        asyncio.run(ch.start())
        try:
            port = ch._server.socket.getsockname()[1]
            with connect(f"ws://127.0.0.1:{port}", open_timeout=5) as ws:
                hello = json.loads(ws.recv(timeout=5))
                assert hello["type"] == "hello"
                assert hello["data"]["session_key"] == "test"

                # 出站：send() 经广播线程推给客户端
                asyncio.run(ch.send(TokenChunk(text="世界")))
                frame = json.loads(ws.recv(timeout=5))
                assert frame["type"] == "token"
                assert frame["data"]["text"] == "世界"

                # 入站：客户端发帧 → listen() 转成 InboundEvent
                ws.send(json.dumps({"type": "user_input", "data": {"text": "hi"}}))
                async def drain() -> str:
                    async for ev in ch.listen():
                        return ev.text
                assert asyncio.run(drain()) == "hi"
        finally:
            asyncio.run(ch.stop())
