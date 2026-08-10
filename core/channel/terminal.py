"""本地 terminal adapter。调试 / 不接 IM 时使用。"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

from core.protocol import (
    FinalMessage,
    InboundEvent,
    StatusChange,
    StreamEvent,
    TokenChunk,
)


class TerminalChannel:
    def __init__(self, session_key: str = "default") -> None:
        self._session_key = session_key
        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._reader: asyncio.Task | None = None

    async def start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._reader:
            await self._reader

    async def _read_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while not self._stop.is_set():
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            text = line.rstrip("\n")
            if text:
                await self._queue.put(
                    InboundEvent(session_key=self._session_key, kind="message", text=text)
                )

    async def listen(self) -> AsyncIterator[InboundEvent]:
        while not self._stop.is_set():
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

    async def send(self, event: StreamEvent) -> None:
        if isinstance(event, TokenChunk):
            sys.stdout.write(event.text)
            sys.stdout.flush()
        elif isinstance(event, FinalMessage):
            sys.stdout.write(event.text + "\n")
            sys.stdout.flush()
        elif isinstance(event, StatusChange):
            sys.stdout.write(f"\n[state: {event.state}]\n")
            sys.stdout.flush()