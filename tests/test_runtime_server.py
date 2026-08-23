"""core/runtime_server.py 单测。

真 ws client（websockets.sync）连真 ws server，验证：
- hello 带 source + session_key → 按 source 注册
- inbound → handler 收到 InboundEvent
- per-session output_q 注册后，consumer 按 last_active_source 路由到 source conn
- 同 source 重连 replace
- 死连接清理
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest
import websockets
from websockets.sync.client import connect as sync_connect

from core.protocol import (
    FinalMessage,
    InboundEvent,
    StatusChange,
    StreamEvent,
    TokenChunk,
)
from core.runtime_server import RuntimeServer, RuntimeServerConfig


def _bind_random_port(server: RuntimeServer) -> int:
    inner = server._server
    if inner is None or not inner.sockets:
        raise RuntimeError("server not started")
    return inner.sockets[0].getsockname()[1]


class ServerHandle:
    """起 RuntimeServer 的小工具，跑在 background thread 的 asyncio loop。

    提供：
    - `loop_input`：所有 inbound 事件汇总（无论 session_key）
    - `register_outbound(session_key, output_q)`：测试 / 上层模拟 SessionManager 注册
    - `dispatch_inbound(ev)`：直接调 handler 模拟 Runtime 主动派发（不进 ws）
    """

    def __init__(
        self,
        default_session_key: str = "default",
        default_source: str = "test",
    ) -> None:
        self.loop_input: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self.output_queues: dict[str, asyncio.Queue[StreamEvent]] = {}
        self.default_session_key = default_session_key
        self.default_source = default_source
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._task: asyncio.Task | None = None
        self.server: RuntimeServer | None = None
        self.port: int = 0

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        cfg = RuntimeServerConfig(host="127.0.0.1", port=0)
        self.server = RuntimeServer(
            cfg,
            default_session_key=self.default_session_key,
            default_source=self.default_source,
        )
        self.server.set_inbound_handler(self._on_inbound)

        async def _starter():
            self._task = asyncio.create_task(self.server.run())
            for _ in range(50):
                if self.server._server is not None:
                    break
                await asyncio.sleep(0.02)
            self.port = _bind_random_port(self.server)

        self._loop.run_until_complete(_starter())
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _on_inbound(self, ev: InboundEvent) -> None:
        await self.loop_input.put(ev)

    def register_outbound(self, session_key: str, output_q: asyncio.Queue[StreamEvent]) -> None:
        if session_key in self.output_queues:
            return
        self.output_queues[session_key] = output_q
        fut = asyncio.run_coroutine_threadsafe(
            self.server.register_outbound_queue(session_key, output_q),
            self._loop,
        )
        fut.result(timeout=2)

    def start(self) -> None:
        self._thread.start()
        for _ in range(100):
            if self.port:
                return
            time.sleep(0.05)
        raise RuntimeError("server failed to start")

    def stop(self) -> None:
        if self.server is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.server.stop(), self._loop).result(timeout=2)
            except Exception:
                pass
        try:
            asyncio.run_coroutine_threadsafe(self._cancel_task(), self._loop).result(timeout=2)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    async def _cancel_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


def _recv_frame(ws, timeout: float = 2.0) -> dict[str, Any]:
    raw = ws.recv(timeout=timeout)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def _hello_frame(session_key: str, source: str, model: str = "") -> dict[str, Any]:
    return {
        "v": 1, "type": "hello", "seq": 0, "ts": 0,
        "data": {"session_key": session_key, "source": source, "model": model},
    }


# ----------------------------------------------------------------------
# 入站
# ----------------------------------------------------------------------

def test_hello_then_recv():
    h = ServerHandle(default_session_key="s1")
    h.start()
    try:
        with sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2) as ws:
            ws.send(json.dumps(_hello_frame("s1", "monodesk")))
            ws.send(json.dumps({
                "v": 1, "type": "user_input",
                "seq": 1, "ts": 0,
                "data": {"text": "hello", "session_key": "s1"},
            }))
            ev = asyncio.run_coroutine_threadsafe(
                asyncio.wait_for(h.loop_input.get(), timeout=2),
                h._loop,
            ).result(timeout=3)
            assert ev.kind == "message"
            assert ev.text == "hello"
            assert ev.session_key == "s1"
            assert ev.source == "monodesk"
    finally:
        h.stop()


def test_inbound_default_session_key_when_hello_omits():
    h = ServerHandle(default_session_key="DEFAULT", default_source="UNKNOWN")
    h.start()
    try:
        with sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2) as ws:
            ws.send(json.dumps({
                "v": 1, "type": "user_input",
                "seq": 1, "ts": 0,
                "data": {"text": "hi"},
            }))
            ev = asyncio.run_coroutine_threadsafe(
                asyncio.wait_for(h.loop_input.get(), timeout=2),
                h._loop,
            ).result(timeout=3)
            assert ev.session_key == "DEFAULT"
            assert ev.source == "UNKNOWN"
    finally:
        h.stop()


def test_interrupt_uses_default_session_key_even_if_data_has_one():
    h = ServerHandle(default_session_key="REAL")
    h.start()
    try:
        with sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2) as ws:
            ws.send(json.dumps(_hello_frame("REAL", "monodesk")))
            ws.send(json.dumps({
                "v": 1, "type": "interrupt",
                "seq": 1, "ts": 0,
                "data": {"session_key": "BAD"},
            }))
            ev = asyncio.run_coroutine_threadsafe(
                asyncio.wait_for(h.loop_input.get(), timeout=2),
                h._loop,
            ).result(timeout=3)
            assert ev.kind == "interrupt"
            assert ev.session_key == "REAL"
    finally:
        h.stop()


# ----------------------------------------------------------------------
# 出站：per-session output_q 注册 → last_active_source 单 conn fan-out
# ----------------------------------------------------------------------

def test_fanout_via_last_active_source_to_one_conn():
    """last_active_source 是 conn 注册时记的——第一个 hello 让 Runtime 知道去哪个 conn 发。"""
    h = ServerHandle()
    h.start()
    out_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
    try:
        h.register_outbound("default", out_q)

        with sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2) as ws:
            ws.send(json.dumps(_hello_frame("default", "monodesk")))
            time.sleep(0.1)
            # 推一个 TokenChunk → 走 last_active_source="monodesk" 的 conn
            asyncio.run_coroutine_threadsafe(out_q.put(TokenChunk(text="hi")), h._loop).result(timeout=2)
            f = _recv_frame(ws, timeout=2)
            assert f["type"] == "token"
            assert f["data"]["text"] == "hi"
            assert f["seq"] >= 0
    finally:
        h.stop()


def test_seq_monotonic_per_server():
    h = ServerHandle()
    h.start()
    out_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
    try:
        h.register_outbound("default", out_q)
        with sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2) as ws:
            ws.send(json.dumps(_hello_frame("default", "monodesk")))
            time.sleep(0.1)
            seqs: list[int] = []
            for i in range(5):
                asyncio.run_coroutine_threadsafe(out_q.put(TokenChunk(text=f"t{i}")), h._loop).result(timeout=2)
                f = _recv_frame(ws, timeout=2)
                seqs.append(f["seq"])
            assert seqs == sorted(seqs)
            assert len(set(seqs)) == 5
    finally:
        h.stop()


def test_fanout_only_to_last_active_source_session_key():
    """同 session_key 两个 source——last_active 决定 fan-out 目标。"""
    h = ServerHandle()
    h.start()
    out_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
    try:
        h.register_outbound("default", out_q)
        ws1 = sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2)
        ws1.send(json.dumps(_hello_frame("default", "monodesk")))
        time.sleep(0.1)
        ws2 = sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2)
        ws2.send(json.dumps(_hello_frame("default", "terminal")))
        time.sleep(0.1)

        # ws2 是后注册的 → last_active_source="terminal"
        asyncio.run_coroutine_threadsafe(out_q.put(StatusChange(state="thinking")), h._loop).result(timeout=2)

        f2 = _recv_frame(ws2, timeout=2)
        assert f2["type"] == "status" and f2["data"]["state"] == "thinking"
        # ws1 应该收不到
        with pytest.raises(Exception):
            ws1.recv(timeout=0.3)
        ws1.close(); ws2.close()
    finally:
        h.stop()


def test_different_session_keys_each_get_their_events():
    """不同 session_key 各自有 output_q consumer——互不干扰。"""
    h = ServerHandle()
    h.start()
    out1: asyncio.Queue[StreamEvent] = asyncio.Queue()
    out2: asyncio.Queue[StreamEvent] = asyncio.Queue()
    try:
        h.register_outbound("s1", out1)
        h.register_outbound("s2", out2)
        ws1 = sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2)
        ws1.send(json.dumps(_hello_frame("s1", "monodesk")))
        ws2 = sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2)
        ws2.send(json.dumps(_hello_frame("s2", "feishu")))
        time.sleep(0.1)

        asyncio.run_coroutine_threadsafe(out1.put(StatusChange(state="thinking")), h._loop).result(timeout=2)
        asyncio.run_coroutine_threadsafe(out2.put(StatusChange(state="thinking")), h._loop).result(timeout=2)

        f1 = _recv_frame(ws1, timeout=2)
        f2 = _recv_frame(ws2, timeout=2)
        assert f1["type"] == "status"
        assert f2["type"] == "status"
        ws1.close(); ws2.close()
    finally:
        h.stop()


def test_same_session_source_replaces_old():
    """同 (sk, src) 重连 → close 旧。"""
    h = ServerHandle()
    h.start()
    out_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
    try:
        h.register_outbound("default", out_q)
        ws1 = sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2)
        ws1.send(json.dumps(_hello_frame("default", "monodesk")))
        time.sleep(0.1)
        ws2 = sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2)
        ws2.send(json.dumps(_hello_frame("default", "monodesk")))
        time.sleep(0.2)

        # ws1 被 close → ws2 是当前注册的
        asyncio.run_coroutine_threadsafe(out_q.put(TokenChunk(text="x")), h._loop).result(timeout=2)
        f2 = _recv_frame(ws2, timeout=2)
        assert f2["type"] == "token"
        with pytest.raises(Exception):
            ws1.recv(timeout=0.3)
        ws1.close(); ws2.close()
    finally:
        h.stop()


def test_drop_dead_client():
    h = ServerHandle()
    h.start()
    out_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
    try:
        h.register_outbound("default", out_q)
        ws1 = sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2)
        ws1.send(json.dumps(_hello_frame("default", "monodesk")))
        time.sleep(0.1)
        ws1.close()
        time.sleep(0.2)
        # send 失败 → _clients 应清掉 source="monodesk"
        asyncio.run_coroutine_threadsafe(out_q.put(TokenChunk(text="x")), h._loop).result(timeout=2)
        # 给 consumer 时间尝试 + 清掉
        time.sleep(0.2)
        # 不应再有 "monodesk" 的 conn
        assert "monodesk" not in h.server._clients
    finally:
        h.stop()


def test_final_message_routed_to_last_active_source():
    h = ServerHandle()
    h.start()
    out_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
    try:
        h.register_outbound("default", out_q)
        with sync_connect(f"ws://127.0.0.1:{h.port}", open_timeout=2) as ws:
            ws.send(json.dumps(_hello_frame("default", "monodesk")))
            time.sleep(0.1)
            asyncio.run_coroutine_threadsafe(out_q.put(FinalMessage(text="done")), h._loop).result(timeout=2)
            f = _recv_frame(ws, timeout=2)
            assert f["type"] == "final"
            assert f["data"]["text"] == "done"
    finally:
        h.stop()


def test_active_sessions_lists_registered():
    h = ServerHandle()
    h.start()
    try:
        out1 = asyncio.Queue(); out2 = asyncio.Queue()
        h.register_outbound("a", out1)
        h.register_outbound("b", out2)
        assert set(h.server.active_sessions()) == {"a", "b"}
        asyncio.run_coroutine_threadsafe(
            h.server.unregister_outbound_queue("a"), h._loop
        ).result(timeout=2)
        assert set(h.server.active_sessions()) == {"b"}
    finally:
        h.stop()