"""Local terminal channel adapter — Rich + prompt_toolkit.

输出 (Rich):
  TokenChunk:        流式打印
  ToolStart:         Panel 卡片（cyan border）
  ToolEnd:           Syntax 高亮 stdout + 非 0 exit 红字
  FinalMessage:      metrics 摘要 + 空行分隔
  ReasoningChunk:    静默
  StatusChange:      静默
  MetricChunk:       静默

输入 (prompt_toolkit):
  - 完整 line editor（光标移动、删除、history 上下、自动补全）
  - history 持久化到 .monox/history
  - patch_stdout 让 Rich 流式输出不破坏 prompt 位置
  - cyan ❯ prompt
  - exit / quit / Ctrl+C 优雅退出
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from core.channel.base import Channel
from core.protocol import (
    FinalMessage,
    InboundEvent,
    StreamEvent,
    TokenChunk,
    ToolEnd,
    ToolStart,
)


_MAX_TOOL_OUTPUT_CHARS = 2000
_HISTORY_PATH = Path.home() / ".monox" / "history"


def _prompt_message() -> FormattedText:
    return FormattedText([("class:prompt", "❯ ")])


class TerminalChannel:
    def __init__(self, session_key: str = "default") -> None:
        self._session_key = session_key
        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._reader: asyncio.Task | None = None
        self.console = Console()
        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._prompt_session = PromptSession(
            history=FileHistory(str(_HISTORY_PATH)),
            message=_prompt_message,
        )

    async def start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._reader:
            await self._reader

    async def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with patch_stdout():
                    text = await self._prompt_session.prompt_async()
            except (EOFError, KeyboardInterrupt):
                self._stop.set()
                break
            text = text.strip()
            if not text:
                continue
            if text.lower() in ("exit", "quit"):
                self._stop.set()
                break
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
            self.console.print(event.text, end="", highlight=False)
        elif isinstance(event, ToolStart):
            self.console.print()
            self.console.print(
                Panel(
                    f"[bold cyan]{event.name}[/bold cyan] {_fmt_args(event.args)}",
                    border_style="cyan",
                    padding=(0, 1),
                    title="tool",
                    title_align="left",
                )
            )
        elif isinstance(event, ToolEnd):
            self._write_tool_result(event.result.stdout, event.result.exit_code)
        elif isinstance(event, FinalMessage):
            self._write_final_metrics(event.metrics)
            self.console.print()

    def _write_tool_result(self, stdout: str, exit_code: int) -> None:
        out = stdout.rstrip("\n")
        if out:
            if len(out) > _MAX_TOOL_OUTPUT_CHARS:
                hidden = len(out) - _MAX_TOOL_OUTPUT_CHARS
                out = out[:_MAX_TOOL_OUTPUT_CHARS] + f"\n... ({hidden} more chars)"
            self.console.print(
                Syntax(out, "bash", theme="monokai", background_color="default", padding=(0, 1))
            )
        if exit_code != 0:
            self.console.print(f"[red]exit {exit_code}[/red]")

    def _write_final_metrics(self, metrics: dict) -> None:
        if not metrics:
            return
        tool_calls = metrics.get("total_tool_calls", 0)
        if tool_calls > 0:
            self.console.print(
                f"[dim]{tool_calls} tools · {metrics.get('total_latency_ms', 0)}ms[/dim]"
            )


def _fmt_args(args: dict) -> str:
    return " ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())