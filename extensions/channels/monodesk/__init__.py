"""MonoDesk desktop channel adapter — in-process ws server for the desktop client。

在 Gateway 进程内启动：监听本地 ws 端口给 MonoDesk desktop client 连。
Frame 协议由 `core.protocol.wire_frames` 提供；本模块只负责：
- 启动 ws server（同步线程）
- listen()：从 desktop 收帧 → InboundEvent yield 给 Gateway
- send()：StreamEvent → 帧 → 广播给所有 desktop 客户端

`MonoDeskChannel.send()` 接受可选 `seq` 参数（Gateway 从 Runtime 下行帧透传）。
不传时由本地 adapter 自增 seq（用于测试 / 直接驱动 channel 场景）。
"""
from __future__ import annotations

import asyncio
import itertools
import queue
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from core.channel.base import Channel
from core.protocol import InboundEvent, StreamEvent
from core.protocol.wire_frames import (
    decode,
    encode,
    from_frame,
    hello_frame,
    to_frame,
)


@dataclass
class MonoDeskChannelConfig:
    """Gateway 进程内 monoDesk adapter 监听的 ws server 配置（给 desktop client 连）。

    默认端口 8766 避开 Runtime 的 8765。允许多 desktop 客户端同时连（共享一个 session）。
    """
    host: str = "127.0.0.1"
    port: int = 8766
    model: str = ""  # hello 帧回传，客户端据此展示当前模型


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
        self._seq = itertools.count()  # 本地 seq（无 runtime seq 时用）

        self._in_q: queue.Queue[InboundEvent] = queue.Queue()
        # 帧元组 (seq_or_None, frame)；seq_or_None 为 None 时广播线程用本地自增 seq
        self._out_q: queue.Queue[tuple[int | None, dict[str, Any]] | None] = queue.Queue()

        # 异步事件唤醒：recv 线程往 _in_q put 后 set()，listen() 立即醒来
        self._in_wake = threading.Event()

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
        # 在 executor 等 started set
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, self._started.wait, 10.0)
        if not ok:
            raise RuntimeError("MonoDesk WebSocket failed to bind within 10s")

    def _serve(self) -> None:
        from websockets.sync.server import serve

        server = serve(self._on_connect, self._cfg.host, self._cfg.port)
        self._server = server
        self._started.set()
        server.serve_forever()

    async def stop(self) -> None:
        self._stop.set()
        self._stop_threads.set()
        self._out_q.put_nowait(None)
        self._in_wake.set()
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
            self._send_frame(ws, hello_frame(self._session_key, self._cfg.model, seq=next(self._seq)))
            with self._clients_lock:
                self._clients.add(ws)
            for raw in ws:
                self._dispatch_inbound(raw)
        except Exception:
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(ws)

    def _dispatch_inbound(self, raw: str | bytes) -> None:
        payload = decode(raw)
        ev = from_frame(
            payload,
            default_session_key=self._session_key,
            default_source="monodesk",
        )
        if ev is None:
            return
        self._in_q.put_nowait(ev)
        self._in_wake.set()

    async def listen(self) -> AsyncIterator[InboundEvent]:
        while not self._stop.is_set():
            await self._wait_wake()
            if self._stop.is_set():
                break
            self._in_wake.clear()
            while not self._stop.is_set():
                try:
                    yield self._in_q.get_nowait()
                except queue.Empty:
                    break

    async def _wait_wake(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._in_wake.wait, 0.1)

    def _wake_for_test(self) -> None:
        self._in_wake.set()

    # ------------------------------------------------------------------
    # 出站：StreamEvent → WS 帧
    # ------------------------------------------------------------------

    async def send(self, event: StreamEvent, seq: int | None = None) -> None:
        """StreamEvent → 帧 → 出站队列。

        seq：可选；Gateway 透传 Runtime 下行帧的 seq，让 desktop 看到 Runtime 单调 seq。
        不传时用本地自增 seq。
        """
        if seq is None:
            seq = next(self._seq)
        frame = to_frame(event, seq=seq)
        if frame is not None:
            self._out_q.put_nowait((None, frame))

    def send_frame_direct(self, seq: int, frame: dict[str, Any]) -> None:
        """Gateway 用：直接把 Runtime 下行帧（带 Runtime seq）转发给 desktop。

        广播线程拿 frame 直接发，不重写 seq。
        """
        self._out_q.put_nowait((None, frame))

    # ------------------------------------------------------------------
    # 广播线程
    # ------------------------------------------------------------------

    def _broadcast_loop(self) -> None:
        while not self._stop_threads.is_set():
            try:
                item = self._out_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            _, frame = item
            payload = encode(frame)

            with self._clients_lock:
                targets = list(self._clients)

            dead: list[Any] = []
            for ws in targets:
                try:
                    ws.send(payload)
                except Exception:
                    dead.append(ws)
            if dead:
                with self._clients_lock:
                    for ws in dead:
                        self._clients.discard(ws)

    def _send_frame(self, ws: Any, frame: dict[str, Any]) -> None:
        ws.send(encode(frame))