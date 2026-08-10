"""Local terminal channel adapter.

Output style:
  TokenChunk:    stream directly
  ToolStart:     newline + indent + name(args)
  ToolEnd:       indent + stdout lines (truncated if huge)
  FinalMessage:  short metrics summary + blank line separator
  Other events:  silent (ReasoningChunk / StatusChange / MetricChunk / Card / ErrorEvent)
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator

from core.channel.base import Channel
from core.protocol import (
    FinalMessage,
    InboundEvent,
    StreamEvent,
    TokenChunk,
    ToolEnd,
    ToolStart,
)


_MAX_TOOL_OUTPUT_LINES = 50


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
            text = line.rstrip("\n").strip()
            if text.lower() in ("exit", "quit"):
                self._stop.set()
                break
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
        elif isinstance(event, ToolStart):
            sys.stdout.write(f"\n  > {event.name}({_fmt_args(event.args)})\n")
            sys.stdout.flush()
        elif isinstance(event, ToolEnd):
            self._write_tool_output(event.result.stdout)
        elif isinstance(event, FinalMessage):
            self._write_final_metrics(event.metrics)
            sys.stdout.write("\n")
            sys.stdout.flush()

    @staticmethod
    def _write_tool_output(stdout: str) -> None:
        out = stdout.rstrip("\n")
        if not out:
            sys.stdout.flush()
            return
        lines = out.split("\n")
        if len(lines) > _MAX_TOOL_OUTPUT_LINES:
            hidden = len(lines) - _MAX_TOOL_OUTPUT_LINES + 1
            lines = lines[:_MAX_TOOL_OUTPUT_LINES - 1] + [f"... ({hidden} more lines)"]
        for line in lines:
            sys.stdout.write(f"    {line}\n")
        sys.stdout.flush()

    @staticmethod
    def _write_final_metrics(metrics: dict) -> None:
        if not metrics:
            return
        tool_calls = metrics.get("total_tool_calls", 0)
        if tool_calls > 0:
            sys.stdout.write(
                f"  [{tool_calls} tools, {metrics.get('total_latency_ms', 0)}ms]\n"
            )


def _fmt_args(args: dict) -> str:
    return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())