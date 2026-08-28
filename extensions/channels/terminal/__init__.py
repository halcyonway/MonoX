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
import sys
import time
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
from core.protocol.wire_frames import FrameType


_MAX_TOOL_OUTPUT_CHARS = 2000
_HISTORY_PATH = Path.home() / ".monox" / "history"


def _prompt_message() -> FormattedText:
    return FormattedText([("class:prompt", "❯ ")])


class _RichStdout:
    """Thin wrapper around the real stdout for use as Rich's file target.

    prompt_toolkit's `patch_stdout` replaces `sys.stdout` with a proxy whose
    `isatty()` returns False, which makes Rich emit plain text without ANSI
    escapes. We bypass that proxy by binding Rich directly to the real fd,
    so colors / cursor control sequences always reach the terminal.

    Writes here do NOT participate in patch_stdout's prompt redraw — which
    is intentional: Rich (Live, Spinner, Panel) owns the screen during output
    and prompt_toolkit redraws the prompt once output stops.
    """

    def __init__(self) -> None:
        self._f = sys.__stdout__

    def write(self, data: str) -> int:
        return self._f.write(data)

    def flush(self) -> None:
        self._f.flush()

    def isatty(self) -> bool:
        return self._f.isatty()

    def fileno(self) -> int:
        return self._f.fileno()

    @property
    def closed(self) -> bool:
        return self._f.closed


class TerminalChannel:
    def __init__(self, session_key: str = "default", debug: bool = False) -> None:
        self._session_key = session_key
        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        # 原始上行帧（/cancel → async_task_cancel）；_runtime.pump_raw_inbound 消费
        self.raw_outbound: asyncio.Queue[dict] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._reader: asyncio.Task | None = None
        self.console = Console(file=_RichStdout())
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
            if text.startswith("/cancel "):
                task_id = text[len("/cancel "):].strip()
                if task_id:
                    await self.raw_outbound.put({
                        "v": 1,
                        "type": FrameType.ASYNC_TASK_CANCEL,
                        "seq": 0,
                        "ts": time.time(),
                        "data": {"task_id": task_id, "reason": "user"},
                    })
                    self.console.print(f"[dim]→ cancel {task_id}[/dim]")
                continue
            await self._queue.put(
                InboundEvent(
                    session_key=self._session_key,
                    kind="message",
                    text=text,
                    source="terminal",
                    event_type="user-input",
                    timestamp=time.time(),
                )
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

    async def handle_raw_frame(self, frame: dict) -> None:
        """async_task_* 帧 → 折叠打印（够用而非完美；完整流看 MonoDesk）。

        terminal 是单线程 console，handler 在 ws recv loop 里直接打印——与 Rich
        输出同管道，无跨线程问题。
        """
        ftype = frame.get("type")
        if ftype == FrameType.ASYNC_TASK_CREATED:
            d = frame.get("data") or {}
            self._stop_spinner()
            self.console.print(
                f"[dim cyan][task {d.get('task_id', '?')}] {d.get('kind', 'subagent')}"
                f" started: \"{d.get('description', '')}\"[/dim cyan]"
            )
        elif ftype == FrameType.ASYNC_TASK_EVENT:
            d = frame.get("data") or {}
            inner = d.get("event") or {}
            task_id = d.get("task_id", "?")
            if inner.get("type") == "tool_end":
                ed = inner.get("data") or {}
                result = ed.get("result") or {}
                self.console.print(
                    f"[dim][task {task_id}]   {ed.get('name', '?')}"
                    f" · {ed.get('latency_ms', 0)}ms · exit {result.get('exit_code', '?')}[/dim]"
                )
            elif inner.get("type") == "error":
                ed = inner.get("data") or {}
                self.console.print(
                    f"[red][task {task_id}]   error: {ed.get('msg', '?')}[/red]"
                )
            # token / reasoning / status 不打印（折叠语义：终态见 async_task_status）
        elif ftype == FrameType.ASYNC_TASK_STATUS:
            d = frame.get("data") or {}
            status = d.get("status", "?")
            dur = d.get("duration_sec") or 0.0
            line = f"[task {d.get('task_id', '?')}] {status} in {dur:.0f}s"
            if status == "failed" and d.get("error"):
                line += f" — {d['error']}"
            elif status in ("cancelled", "timed_out") and d.get("cancel_reason"):
                line += f" ({d['cancel_reason']})"
            elif status == "completed" and d.get("final_text"):
                preview = d["final_text"].replace("\n", " ")[:200]
                line += f" — {preview}"
            style = "green" if status == "completed" else "red" if status == "failed" else "yellow"
            self._stop_spinner()
            self.console.print(f"[{style}]{line}[/{style}]")

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