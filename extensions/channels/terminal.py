"""Local terminal channel adapter — Rich + prompt_toolkit.

两档:
  normal (默认):   TokenChunk / ReasoningChunk / ToolStart / ToolEnd / FinalMessage
                   推理是基本 output，正常用户也想看模型在想什么
  debug (--debug):  上述 + StatusChange + MetricChunk
                   状态切换 + step / latency / tokens 详情

启动:
  uv run python run.py            # normal
  uv run python run.py --debug    # debug

运行时:
  F12              切换 debug 视图（控制台提示 on/off；不影响 reasoning）
  Ctrl+C           优雅退出

输出 (Rich):
  TokenChunk:        流式打印
  ReasoningChunk:    流式累积 + 整体 flush（grey50 italic 💭）— normal 也显示
  ToolStart:         Panel 卡片（cyan border）
  ToolEnd:           Syntax 高亮 stdout + 非 0 exit 红字
  FinalMessage:      metrics 摘要 + 空行分隔
  StatusChange:      [debug] thinking 时显示 spinner（⏳ + 状态名）；其他状态行
  MetricChunk:       [debug] step / latency / tokens 详情（dim）

输入 (prompt_toolkit):
  - 完整 line editor + 持久 history + patch_stdout 跟 Rich 流式输出共存
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax

from core.channel.base import Channel
from core.protocol import (
    FinalMessage,
    InboundEvent,
    MetricChunk,
    ReasoningChunk,
    StatusChange,
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
    def __init__(self, session_key: str = "default", debug: bool = False) -> None:
        self._session_key = session_key
        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._reader: asyncio.Task | None = None
        self.console = Console()
        self._debug = debug
        self._reasoning_buf = ""
        self._spinner: Live | None = None
        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._prompt_session = PromptSession(
            history=FileHistory(str(_HISTORY_PATH)),
            message=_prompt_message,
            key_bindings=self._build_keybindings(),
        )

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("f12")
        def _toggle_debug(event) -> None:
            self._debug = not self._debug
            self._stop_spinner()
            label = "on" if self._debug else "off"
            # patch_stdout 在 prompt 内包着，直接 console.print 会打乱 prompt
            self.console.print(f"\n[dim]debug view: {label}[/dim]")
            event.app.invalidate()

        return kb

    def _start_spinner(self, state: str) -> None:
        if self._spinner is not None:
            return
        self._spinner = Live(
            Spinner("dots", text=f" {state}", style="bold magenta"),
            console=self.console,
            transient=True,
            refresh_per_second=12,
        )
        self._spinner.start()

    def _stop_spinner(self) -> None:
        if self._spinner is None:
            return
        self._spinner.stop()
        self._spinner = None

    async def start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        self._stop.set()
        self._stop_spinner()
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
        # Reasoning 流式累积（normal 也显示，flush 到下一个非 reasoning event）
        if isinstance(event, ReasoningChunk):
            self._reasoning_buf += event.text
            return

        # 切换到非 reasoning，先 flush buffer
        if self._reasoning_buf:
            self.console.print(
                f"[grey50 italic]💭 {self._reasoning_buf.rstrip()}[/grey50 italic]"
            )
            self._reasoning_buf = ""

        if isinstance(event, TokenChunk):
            self._stop_spinner()
            self.console.print(event.text, end="", highlight=False)
        elif isinstance(event, ToolStart):
            self._stop_spinner()
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
            self._stop_spinner()
            self._write_tool_result(event.result.stdout, event.result.exit_code)
        elif isinstance(event, StatusChange):
            if not self._debug:
                self._stop_spinner()
                return
            if event.state == "thinking":
                self._start_spinner(event.state)
            else:
                self._stop_spinner()
                self.console.print(f"[bold magenta]⟫ {event.state}[/bold magenta]")
        elif isinstance(event, MetricChunk):
            if self._debug:
                self._stop_spinner()
                self._print_metric(event.metrics)
        elif isinstance(event, FinalMessage):
            self._stop_spinner()
            self._write_final_metrics(event.metrics)
            self.console.print()

    def _print_metric(self, m: dict) -> None:
        tokens = m.get("tokens") or {}
        parts = [
            f"step={m.get('step_idx')}",
            f"latency={m.get('latency_ms')}ms",
            f"tools={m.get('tool_calls_count', 0)}",
        ]
        if tokens:
            parts.append(f"in={tokens.get('prompt_tokens', '?')}")
            parts.append(f"out={tokens.get('completion_tokens', '?')}")
        self.console.print(f"  [dim]{' '.join(parts)}[/dim]")

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