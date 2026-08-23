"""Runtime ws server（多 session + 多 source 形态）。

Runtime 进程对外只暴露 ws server，每个 ws conn 代表一个 channel 进程。
ws conn 用 `source`（channel 名）索引——一个 channel 一条连接，服务该 channel 的
所有 session_key。Runtime 不感知 channel 协议——把 InboundEvent 派发给注册的
inbound handler，把 StreamEvent 按 `(session_key, last_active_source)` 路由到
source 对应的 ws conn。

    Channel 进程 ──ws(user_input/command/interrupt)──► Runtime
                  ◄─ws(status/token/.../final/error)──

设计：
- `_clients: dict[source, ws]`，同 source 第二个连接进来 → 主动 close 旧连接（防同 channel 双开）
- session_key 用 `channel_name:channel_session_id` 全局唯一，故连接无需按 session_key 索引
- `last_active_source[session_key]` 追踪该 session 最近上行的 source；fan-out 时按 source 找 conn
- session 销毁（idle destroy）时由 `unregister_outbound_queue` 清掉对应 last_active 条目
- hello 帧 schema：`data.session_key` / `data.source`——`session_key` 作为该 channel 的
  default session_key（帧缺 session_key 时回退用）；`source` 作为连接索引 key
"""
from __future__ import annotations

import asyncio
import itertools
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from core.protocol import InboundEvent, StreamEvent
from core.protocol.wire_frames import decode, frame_to_stream_event, from_frame, to_frame


@dataclass(frozen=True)
class RuntimeServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    max_clients: int = 16


InboundHandler = Callable[[InboundEvent], Awaitable[None]]
# session_key → 从 SessionLoop.output_q 取 event 后怎么发（RuntimeServer 内部实现）。
# 这里仅是 consumer task 的 in-q 注册，框架不暴露给外部回调。
OutboundConsumer = asyncio.Task[None]


class RuntimeServer:
    """Runtime 端 ws server。

    用法（装配时）：
        server = RuntimeServer(cfg)
        server.set_inbound_handler(session_manager.dispatch_inbound)
        server.register_outbound_queue("default", output_q)
        asyncio.gather(server.run(), session_manager.run())

    inbound handler 由外部 SessionManager 提供（经 set_inbound_handler 注入）；
    RuntimeServer 不持有 SessionManager 引用。outbound per-session output_q 由 SessionManager
    在 create SessionLoop 后注册。
    """

    def __init__(
        self,
        cfg: RuntimeServerConfig,
        *,
        default_session_key: str = "default",
        default_source: str = "unknown",
    ) -> None:
        self._cfg = cfg
        self._default_session_key = default_session_key
        self._default_source = default_source

        # source（channel 名）→ ws conn。一个 channel 一条连接，服务其所有 session_key。
        self._clients: dict[str, ServerConnection] = {}
        self._last_active_source: dict[str, str] = {}
        self._clients_lock = asyncio.Lock()

        # inbound handler 单一注册点；不设置则 inbound 直接被丢（用于测试）
        self._inbound_handler: InboundHandler | None = None

        # 每 session_key 一个 outbound consumer task（拉 output_q → 按 last_active_source 发）
        self._outbound_qs: dict[str, asyncio.Queue[StreamEvent | None]] = {}
        self._outbound_consumers: dict[str, asyncio.Task[None]] = {}

        self._seq = itertools.count()
        self._stop = asyncio.Event()
        self._server: Any = None
        self._running = False

    # ------------------------------------------------------------------
    # 公共 API（装配用）
    # ------------------------------------------------------------------

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        """设置 inbound 派发回调。SessionManager 提供 `dispatch_inbound`。"""
        self._inbound_handler = handler

    async def register_outbound_queue(self, session_key: str, output_q: asyncio.Queue[StreamEvent]) -> None:
        """注册 per-session output_q；RuntimeServer 启动 consumer task 按 last_active_source 路由。

        必须在 RuntimeServer 的 asyncio loop 内调用（await this method, or schedule via
        `asyncio.run_coroutine_threadsafe` from another thread）。
        """
        if session_key in self._outbound_qs:
            return  # 已注册
        self._outbound_qs[session_key] = output_q
        task = asyncio.create_task(self._outbound_consumer(session_key, output_q))
        self._outbound_consumers[session_key] = task

    async def unregister_outbound_queue(self, session_key: str) -> None:
        """注销 per-session output_q；cancel 对应 consumer task。"""
        self._outbound_qs.pop(session_key, None)
        task = self._outbound_consumers.pop(session_key, None)
        if task is not None:
            task.cancel()
            self._last_active_source.pop(session_key, None)

    def active_sessions(self) -> list[str]:
        """当前注册了 outbound queue 的 session_key 列表。"""
        return sorted(self._outbound_qs.keys())

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def run(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            async with serve(
                self._on_connect,
                self._cfg.host,
                self._cfg.port,
                max_size=2**20,
            ) as server:
                self._server = server
                try:
                    await self._stop.wait()
                finally:
                    self._server = None
        except OSError as e:
            raise RuntimeError(f"RuntimeServer bind failed on {self._cfg.host}:{self._cfg.port}: {e}")

    async def stop(self) -> None:
        self._stop.set()
        # 唤醒所有 outbound consumer（push None 进去让它们跳出循环）
        for q in self._outbound_qs.values():
            try:
                q.put_nowait(None)  # type: ignore[arg-type]
            except Exception:
                pass
        # cancel 所有 consumer
        for task in self._outbound_consumers.values():
            task.cancel()
        # close 所有 ws
        async with self._clients_lock:
            for ws in list(self._clients.values()):
                try:
                    await ws.close(code=1001, reason="server stopping")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # inbound: ws connect → hello → 注册 → recv loop → handler
    # ------------------------------------------------------------------

    async def _on_connect(self, ws: ServerConnection) -> None:
        async with self._clients_lock:
            if len(self._clients) >= self._cfg.max_clients:
                await ws.close(code=1013, reason="max clients reached")
                return

        session_key = self._default_session_key
        source = self._default_source
        first_ev: InboundEvent | None = None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        except (asyncio.TimeoutError, ConnectionClosed):
            await ws.close(code=1008, reason="hello timeout")
            return
        except Exception as e:
            await ws.close(code=1011, reason=str(e))
            return

        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")

        first_payload = decode(raw)
        if not isinstance(first_payload, dict):
            await ws.close(code=1008, reason="invalid first frame")
            return

        mtype = first_payload.get("type")
        if mtype == "hello":
            data = first_payload.get("data") or {}
            if isinstance(data, dict):
                sk = data.get("session_key")
                if isinstance(sk, str) and sk:
                    session_key = sk
                src = data.get("source")
                if isinstance(src, str) and src:
                    source = src
            first_ev = None
        else:
            first_ev = from_frame(
                first_payload,
                default_session_key=session_key,
                default_source=source,
            )

        # 注册：按 source 索引；同 source 再连 replace 旧连接
        async with self._clients_lock:
            old = self._clients.get(source)
            if old is not None and old is not ws:
                try:
                    await old.close(code=1011, reason="replaced")
                except Exception:
                    pass
            self._clients[source] = ws
            # 该 channel 的 default session 视为隐式 last_active（让首次连接后产生的下行事件有出口）
            self._last_active_source[session_key] = source

        if first_ev is not None:
            await self._dispatch_inbound(first_ev)

        try:
            await self._recv_loop(ws, session_key, source)
        finally:
            async with self._clients_lock:
                if self._clients.get(source) is ws:
                    del self._clients[source]

    async def _recv_loop(self, ws: ServerConnection, session_key: str, source: str) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                payload = decode(raw)
                ev = from_frame(
                    payload,
                    default_session_key=session_key,
                    default_source=source,
                )
                if ev is not None:
                    await self._dispatch_inbound(ev)
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            sys.stderr.write(f"[RuntimeServer] recv error: {e}\n")
            sys.stderr.flush()

    async def _dispatch_inbound(self, ev: InboundEvent) -> None:
        if self._inbound_handler is None:
            return
        await self._inbound_handler(ev)
        if ev.source:
            self._last_active_source[ev.session_key] = ev.source

    # ------------------------------------------------------------------
    # outbound: per-session consumer task → 按 last_active_source 路由
    # ------------------------------------------------------------------

    async def _outbound_consumer(self, session_key: str, output_q: asyncio.Queue[StreamEvent | None]) -> None:
        while True:
            ev = await output_q.get()
            if ev is None or self._stop.is_set():
                return
            src = self._last_active_source.get(session_key)
            if src is None:
                continue  # 无活跃 source → 丢弃
            async with self._clients_lock:
                ws = self._clients.get(src)
            if ws is None:
                continue  # 该 source 已断开 → 丢弃
            frame = to_frame(ev, seq=next(self._seq))
            if frame is None:
                continue
            payload = json.dumps(frame, ensure_ascii=False, default=str)
            try:
                await ws.send(payload)
            except Exception as e:
                sys.stderr.write(f"[RuntimeServer] send error: {e}\n")
                sys.stderr.flush()
                # send 失败 → 该 conn 已死；清理
                async with self._clients_lock:
                    if self._clients.get(src) is ws:
                        del self._clients[src]
