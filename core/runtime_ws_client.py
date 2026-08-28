"""RuntimeWSClient — channel 进程侧 ws client ↔ Runtime。

每个 channel 进程启动一个实例，通过 ws 连 RuntimeServer（`:8765`）。
负责：
- 发 hello 帧（带 `data.source` + `data.session_key`，让 Runtime 按 (sk, src) 索引）
- 把 channel.listen() 的 InboundEvent 编码为入站帧投到 out_q
- 把 Runtime 下行帧解码为 StreamEvent 给 channel.send()

连接断 → 指数退避重连（0.5/1/2/4/8/16s），不 crash。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect as ws_connect
from websockets.exceptions import ConnectionClosed

from core.protocol import StreamEvent
from core.protocol.wire_frames import decode, frame_to_stream_event

_log = logging.getLogger("monox.runtime_ws_client")


class RuntimeWSClient:
    BACKOFF = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)

    def __init__(
        self,
        *,
        url: str,
        hello_session_key: str,
        hello_source: str,
    ) -> None:
        self._url = url
        # 内联构造 hello dict（不动 wire_frames.hello_frame 公共签名）。
        # hello_source 必填：Runtime 用 (session_key, source) 索引 ws conn，
        # 缺 source 时 Runtime fallback "unknown"，多 channel 共用 sk 会撞 key。
        # 不传 model——model 是 Runtime 关心的事，channel 不参与模型选择。
        self._hello = {
            "v": 1, "type": "hello",
            "seq": 0, "ts": 0,
            "data": {
                "session_key": hello_session_key,
                "source": hello_source,
            },
        }
        self._out_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._in_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
        # 原始帧旁路：async_task_* 等 wire 层专用帧不进 StreamEvent（frame_to_stream_event
        # 返回 None 会被丢）——想要它们的 channel 注入 handler，每条下行帧先喂它
        self._raw_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._ws: ClientConnection | None = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()

    def set_raw_frame_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        self._raw_handler = handler

    async def send(self, frame: dict[str, Any]) -> None:
        await self._out_q.put(frame)

    async def recv(self) -> StreamEvent:
        return await self._in_q.get()

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                async with ws_connect(self._url, max_size=2**20) as ws:
                    self._ws = ws
                    await ws.send(json.dumps(self._hello, ensure_ascii=False, default=str))
                    self._connected.set()
                    attempt = 0
                    await asyncio.gather(
                        self._send_loop(ws),
                        self._recv_loop(ws),
                    )
            except (OSError, ConnectionClosed, websockets.WebSocketException) as e:
                _log.info("runtime ws disconnected: %s", e)
            except Exception as e:
                _log.warning("runtime ws unexpected: %s", e)
            self._connected.clear()
            self._ws = None
            if self._stop.is_set():
                return
            await asyncio.sleep(self.BACKOFF[min(attempt, len(self.BACKOFF) - 1)])
            attempt += 1

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _send_loop(self, ws: ClientConnection) -> None:
        try:
            while not self._stop.is_set():
                frame = await self._out_q.get()
                await ws.send(json.dumps(frame, ensure_ascii=False, default=str))
        except ConnectionClosed:
            pass

    async def _recv_loop(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                payload = decode(raw)
                if self._raw_handler is not None and isinstance(payload, dict):
                    await self._raw_handler(payload)
                ev = frame_to_stream_event(payload)
                if ev is not None:
                    await self._in_q.put(ev)
        except ConnectionClosed:
            pass