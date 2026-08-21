"""MonoDesk desktop channel adapter — WebSocket server for the desktop client.

入站：MonoDesk (Tauri) → WS JSON 帧 → InboundEvent
出站：StreamEvent → WS JSON 帧（NDJSON 信封 {v,type,seq,ts,data}）

WS server 用 websockets.sync.server 在独立线程里跑（同步阻塞 recv/send），
不阻塞 MonoX 的 asyncio 主循环；outbound 帧经 queue.Queue 由广播线程推给客户端。
协议字段严格对齐 MonoDesk/spec/requirements/ws-channel-protocol.md。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import queue
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from core.channel.base import Channel
from core.protocol import (
    Card,
    ErrorEvent,
    FinalMessage,
    InboundEvent,
    MetricChunk,
    ReasoningChunk,
    StatusChange,
    StreamEvent,
    TokenChunk,
    ToolEnd,
    ToolResult,
    ToolStart,
)

_PROTOCOL_VERSION = 1


@dataclass
class MonoDeskChannelConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    model: str = ""  # hello 帧回传，客户端据此展示当前模型


def _decode_bytes(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def _tool_result_to_dict(result: ToolResult) -> dict[str, Any]:
    return {
        "call_id": result.call_id,
        "status": result.status,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "artifacts": [
            {"name": f.name, "mime": f.mime, "content": _decode_bytes(f.content)}
            for f in result.artifacts
        ],
        "truncated": result.truncated,
        "budget_id": result.budget_id,
    }


class MonoDeskChannel:
    def __init__(
        self,
        cfg: MonoDeskChannelConfig,
        session_key: str = "default",
    ) -> None:
        self._cfg = cfg
        self._session_key = session_key
        self._stop = asyncio.Event()
        self._stop_threads = threading.Event()
        self._started = threading.Event()
        self._seq = itertools.count()  # 线程安全的单调递增序号
        self._in_q: queue.Queue[InboundEvent] = queue.Queue()
        self._out_q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._clients: set[Any] = set()
        self._clients_lock = threading.Lock()
        self._server: Any = None
        self._ws_thread: threading.Thread | None = None
        self._broadcast_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._broadcast_thread = threading.Thread(
            target=self._broadcast_loop, daemon=True, name="monodesk-broadcast"
        )
        self._broadcast_thread.start()
        self._ws_thread = threading.Thread(
            target=self._serve, daemon=True, name="monodesk-ws"
        )
        self._ws_thread.start()
        self._started.wait(timeout=10)
        if not self._started.is_set():
            raise RuntimeError("MonoDesk WebSocket failed to bind within 10s")

    def _serve(self) -> None:
        from websockets.sync.server import serve

        server = serve(self._on_connect, self._cfg.host, self._cfg.port)
        self._server = server
        self._started.set()
        server.serve_forever()  # 阻塞，直到 stop() 调 shutdown()

    async def stop(self) -> None:
        self._stop.set()
        self._stop_threads.set()
        self._out_q.put_nowait(None)  # 唤醒广播线程
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
        for t in (self._broadcast_thread, self._ws_thread):
            if t is not None:
                t.join(timeout=3)
        self._ws_thread = self._broadcast_thread = None

    # ------------------------------------------------------------------
    # 入站：WS 帧 → InboundEvent
    # ------------------------------------------------------------------

    def _on_connect(self, ws: Any) -> None:
        try:
            # 先发 hello 再注册广播，保证 hello 是客户端看到的第一帧
            self._send_frame(ws, self._hello())
            with self._clients_lock:
                self._clients.add(ws)
            for raw in ws:  # 迭代 recv()，连接关闭即结束
                self._dispatch_inbound(raw)
        except Exception:
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(ws)

    def _dispatch_inbound(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        data = msg.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        ts = data.get("ts") or time.time()

        if mtype == "user_input":
            ev = InboundEvent(
                session_key=data.get("session_key") or self._session_key,
                kind="message",
                text=data.get("text", ""),
                source="monodesk",
                event_type="user-input",
                timestamp=ts,
                meta=data.get("meta") or {},
            )
        elif mtype == "command":
            ev = InboundEvent(
                session_key=self._session_key,
                kind="command",
                text=data.get("text", ""),
                source="monodesk",
                event_type="command",
                timestamp=ts,
            )
        elif mtype == "interrupt":
            # 引擎目前把所有 inbound 当用户消息 append，真正的打断是 v2 的事
            ev = InboundEvent(
                session_key=self._session_key,
                kind="interrupt",
                text="",
                source="monodesk",
                event_type="interrupt",
                timestamp=ts,
            )
        else:
            return
        self._in_q.put_nowait(ev)

    async def listen(self) -> AsyncIterator[InboundEvent]:
        while not self._stop.is_set():
            while True:
                try:
                    yield self._in_q.get_nowait()
                except queue.Empty:
                    break
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------
    # 出站：StreamEvent → WS 帧
    # ------------------------------------------------------------------

    async def send(self, event: StreamEvent) -> None:
        frame = self._to_frame(event)
        if frame is not None:
            self._out_q.put_nowait(frame)

    def _to_frame(self, event: StreamEvent) -> dict[str, Any] | None:
        if isinstance(event, StatusChange):
            t, d = "status", {"state": event.state}
        elif isinstance(event, TokenChunk):
            t, d = "token", {"text": event.text}
        elif isinstance(event, ReasoningChunk):
            t, d = "reasoning", {"text": event.text}
        elif isinstance(event, ToolStart):
            t, d = "tool_start", {"name": event.name, "args": event.args}
        elif isinstance(event, ToolEnd):
            t, d = "tool_end", {
                "name": event.name,
                "latency_ms": event.latency_ms,
                "result": _tool_result_to_dict(event.result),
            }
        elif isinstance(event, MetricChunk):
            t, d = "metric", {"metrics": event.metrics}
        elif isinstance(event, FinalMessage):
            t, d = "final", {"text": event.text, "metrics": event.metrics}
        elif isinstance(event, Card):
            t, d = "card", {"data": event.data}
        elif isinstance(event, ErrorEvent):
            t, d = "error", {
                "code": event.code,
                "msg": event.msg,
                "retryable": event.retryable,
            }
        else:
            return None
        return {
            "v": _PROTOCOL_VERSION,
            "type": t,
            "seq": next(self._seq),
            "ts": time.time(),
            "data": d,
        }

    # ------------------------------------------------------------------
    # 广播线程
    # ------------------------------------------------------------------

    def _hello(self) -> dict[str, Any]:
        return {
            "v": _PROTOCOL_VERSION,
            "type": "hello",
            "seq": next(self._seq),
            "ts": time.time(),
            "data": {"session_key": self._session_key, "model": self._cfg.model},
        }

    def _broadcast_loop(self) -> None:
        while not self._stop_threads.is_set():
            try:
                frame = self._out_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if frame is None:
                break
            payload = json.dumps(frame, ensure_ascii=False, default=str)
            with self._clients_lock:
                clients = list(self._clients)
            for ws in clients:
                try:
                    ws.send(payload)
                except Exception:
                    with self._clients_lock:
                        self._clients.discard(ws)

    def _send_frame(self, ws: Any, frame: dict[str, Any]) -> None:
        ws.send(json.dumps(frame, ensure_ascii=False, default=str))
