"""Textual-based full-screen chat adapter.

架构：
  TextualChannel（实现 Channel 协议）
    └─ ChatApp（Textual App）
         └─ ChatScreen（Header + VerticalScroll history + Input + StatusBar + DebugPanel）

所有组件跑在同一个 asyncio loop（顶层 asyncio.run）—— send(event) 直接 put_nowait 到
内部 out_q；ChatApp 的 _drain_outbound worker 在同 loop 拉事件，更新 widget。

event 类型 → widget 映射见模块底部 _handle_event。

启动：
  config.toml: [channel] kind = "textual"
  uv run python run.py            # normal
  uv run python run.py --debug    # 初始 debug 面板可见（F12 仍可切换）

退出：
  输入 exit / quit → App 干净退出
  Ctrl+C → 同上
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from textual.app import App, Binding
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Footer, Input, LoadingIndicator, Markdown, Static
from textual import work

from core.protocol import (
    ErrorEvent,
    FinalMessage,
    InboundEvent,
    ReasoningChunk,
    StreamEvent,
    StatusChange,
    TokenChunk,
    ToolEnd,
    ToolStart,
    MetricChunk,
    Card,
)


_MAX_TOOL_OUTPUT_CHARS = 2000


_CSS = """
Screen {
    layout: vertical;
    background: $surface;
}

#header {
    dock: top;
    height: 1;
    background: $primary;
    color: $text;
    text-style: bold;
    padding: 0 1;
}

#history {
    height: 1fr;
    padding: 1 2;
    scrollbar-size: 1 1;
}

#input-row {
    dock: bottom;
    height: 3;
    border-top: solid $primary 50%;
    padding: 0 1;
}

#prompt {
    height: 100%;
    border: none;
}

#status-bar {
    dock: bottom;
    height: 1;
    background: $boost;
    color: $text-muted;
    padding: 0 2;
}

#status-bar.loading {
    color: $warning;
    text-style: bold;
}

#debug-panel {
    dock: right;
    width: 40;
    background: $boost;
    border-left: solid $primary 50%;
    padding: 0 1;
    display: none;
    overflow-y: auto;
}

#debug-panel.visible {
    display: block;
}

UserMessage {
    margin: 1 0 0 0;
    color: $text;
    text-style: bold;
}

AssistantMessage {
    margin: 0 0 1 0;
    color: $text;
}

ReasoningMessage {
    margin: 0 0 1 0;
    color: $text-muted;
    text-style: italic;
}

ToolCard {
    margin: 0 0 1 0;
    border: round $secondary;
    padding: 0 1;
}

.error {
    color: $error;
    text-style: bold;
}

.metric-row {
    color: $text-muted;
}
"""


# =================== Message widgets ===================


class UserMessage(Static):
    """用户消息（加粗 label + 正文）。"""

    def __init__(self, text: str) -> None:
        super().__init__(f"❯ {text}", markup=False)


class AssistantMessage(Markdown):
    """流式 assistant 回复。Markdown 节流刷新：每个 token 不直接 reparse，
    通过 call_after_refresh 把多次 append 合并到下一帧 ~16fps。"""

    def __init__(self) -> None:
        super().__init__(markdown="")
        self._buf = ""
        self._dirty = False
        self._scheduled = False
        self._frozen = False

    def append_token(self, text: str) -> None:
        if self._frozen:
            return
        self._buf += text
        self._dirty = True
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._scheduled or self._frozen:
            return
        self._scheduled = True
        self.call_after_refresh(self._flush)

    def _flush(self) -> None:
        self._scheduled = False
        if self._frozen:
            return
        if self._dirty and self._buf:
            self.update(self._buf)
            self._dirty = False

    def update(self, markdown: str) -> object:  # type: ignore[override]
        """override：忽略空字符串 update（避免 mount 期间 _on_mount 的空 update 抢）。"""
        if not markdown:
            # 返回 awaitable 占位，避免 _on_mount 的 `await self.update("")` 失败
            async def _noop(): return None
            return _noop()
        return super().update(markdown)

    def freeze(self) -> None:
        """冻结：lock 住 buffer，禁用后续 append 触发 update。"""
        self._frozen = True
        # 取消待 flush
        self._scheduled = False
        if self._dirty:
            self.update(self._buf)
            self._dirty = False


class ReasoningMessage(Static):
    """模型思考累积。灰色 italic，flush 后 lock。"""

    def __init__(self) -> None:
        super().__init__("💭 ", markup=False, classes="reasoning")
        self._buf = ""

    def append_chunk(self, text: str) -> None:
        self._buf += text
        self.update(f"💭 {self._buf}")

    def freeze(self) -> None:
        # 已经累积过，flush时只更新一次确保非空
        text = self._buf.rstrip()
        if text:
            self.update(f"💭 {text}")


class ToolCard(Static):
    """Cyan border 工具调用卡片。ToolStart 时挂空壳，ToolEnd 时回填。"""

    def __init__(self, name: str, args: dict) -> None:
        self._header = f"[bold cyan]{name}[/bold cyan] {_fmt_args(args)}"
        super().__init__(self._header, markup=True)

    def complete(self, stdout: str, stderr: str, exit_code: int, latency_ms: int) -> None:
        out = stdout.rstrip("\n")
        if len(out) > _MAX_TOOL_OUTPUT_CHARS:
            hidden = len(out) - _MAX_TOOL_OUTPUT_CHARS
            out = out[:_MAX_TOOL_OUTPUT_CHARS] + f"\n... ({hidden} more chars)"
        body = self._header + "\n" + f"[dim]{out}[/dim]"
        if stderr.strip():
            body += f"\n[yellow]{stderr.strip()}[/yellow]"
        if exit_code != 0:
            body += f"\n[red]exit {exit_code}[/red]"
        body += f"\n[dim]{latency_ms}ms[/dim]"
        self.update(body)


class StatusBar(Static):
    """底部状态行。thinking 时挂 LoadingIndicator，文字加粗。"""

    def __init__(self) -> None:
        super().__init__("idle", id="status-bar")
        self._spinner_mounted = False
        self._state_text = "idle"

    @property
    def state_text(self) -> str:
        return self._state_text

    def set_state(self, state: str) -> None:
        self._state_text = state
        self.update(state)
        if state == "thinking":
            self.add_class("loading")
        else:
            self.remove_class("loading")


class DebugPanel(Static):
    """右侧 debug 面板。F12 切显隐；metric / state 切换写入。"""

    def __init__(self) -> None:
        super().__init__("", id="debug-panel", markup=False)
        self._lines: list[str] = []

    def append(self, line: str) -> None:
        self._lines.append(line)
        # 保留最近 200 行
        if len(self._lines) > 200:
            self._lines = self._lines[-200:]
        self.update("\n".join(self._lines))


# =================== Screen + App ===================


class ChatScreen(Widget):
    """ChatScreen 不是真正的 Screen 子类——本实现用单 Screen + 直接 compose widgets。

    简化：把布局直接放在 App 的 compose 里，避免 Screen push/pop 复杂度。
    """

    pass  # 仅占位；实际 compose 在 ChatApp


class ChatApp(App):
    """Textual App。Compose header / history / input / status / debug panel。

    App 持有 _drain_outbound worker，从 out_q 拉事件并更新 widget。
    """

    CSS = _CSS
    BINDINGS = [
        Binding("f12", "toggle_debug", "Debug"),
        Binding("ctrl+c", "quit_app", "Quit", show=False),
    ]

    def __init__(self, channel: "TextualChannel") -> None:
        super().__init__()
        self._channel = channel
        self._worker: asyncio.Task | None = None
        self._exited = False

        # 流式累积状态
        self._cur_user: UserMessage | None = None
        self._cur_assistant: AssistantMessage | None = None
        self._cur_reasoning: ReasoningMessage | None = None
        self._cur_tool: ToolCard | None = None

    # ----- compose -----

    def compose(self):
        yield Static(f"MonoX · {self._channel._session_key}", id="app-header")
        with VerticalScroll(id="history"):
            # history 容器；动态 mount widgets
            pass
        yield Input(placeholder="input... (Enter to send, F12 debug, Ctrl+C exit)", id="prompt")
        yield StatusBar()
        yield DebugPanel()
        yield Footer()

    # ----- lifecycle -----

    def on_mount(self) -> None:
        # debug 初始可见性
        if self._channel._debug:
            self.query_one("#debug-panel").add_class("visible")
        # 启动 out_q worker（@work 在 App event loop 跑，跟 message pump 一致）
        self._drain_outbound()

    # ----- input → in_q -----

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        event.input.clear()
        self._channel.submit_user_input(text)

    # ----- debug panel toggle -----

    def action_toggle_debug(self) -> None:
        panel = self.query_one("#debug-panel")
        panel.toggle_class("visible")

    def action_quit_app(self) -> None:
        self._exited = True
        self.exit()

    # ----- outbound bridge -----

    @work(exclusive=True, group="drain")
    async def _drain_outbound(self) -> None:
        """App message pump 同 loop 里跑。@work 保证不会 leak across event loops。
        """
        while True:
            ev = await self._channel._out_q.get()
            try:
                self._handle_event(ev)
            except Exception as exc:
                # widget 更新失败不应让 worker 死掉
                self._channel._out_q.put_nowait(
                    ErrorEvent(code="render_error", msg=str(exc), retryable=False)
                )

    def _handle_event(self, ev: StreamEvent) -> None:
        history = self.query_one("#history")
        debug_panel = self.query_one(DebugPanel)

        # reasoning 累积：非 reasoning event 先 flush
        if not isinstance(ev, ReasoningChunk):
            if self._cur_reasoning is not None:
                self._cur_reasoning.freeze()
                self._cur_reasoning = None

        # assistant 累积：tool start / status change / final 等非 token event 先 freeze
        def _freeze_assistant():
            if self._cur_assistant is not None:
                self._cur_assistant.freeze()
                self._cur_assistant = None

        if isinstance(ev, ReasoningChunk):
            if self._cur_reasoning is None:
                self._cur_reasoning = ReasoningMessage()
                history.mount(self._cur_reasoning)
                self._scroll_to_bottom()
            self._cur_reasoning.append_chunk(ev.text)
            return

        if isinstance(ev, TokenChunk):
            if self._cur_assistant is None:
                self._cur_assistant = AssistantMessage()
                history.mount(self._cur_assistant)
                self._scroll_to_bottom()
            self._cur_assistant.append_token(ev.text)
            # 触发一次 flush 把当前 buf 渲染上去（避免 mount 期间 _on_mount 的空 update 抢）
            self._cur_assistant._schedule_flush()
            return

        if isinstance(ev, ToolStart):
            _freeze_assistant()
            self._cur_tool = ToolCard(name=ev.name, args=ev.args)
            history.mount(self._cur_tool)
            self._scroll_to_bottom()
            return

        if isinstance(ev, ToolEnd):
            if self._cur_tool is not None:
                self._cur_tool.complete(
                    stdout=ev.result.stdout,
                    stderr=ev.result.stderr,
                    exit_code=ev.result.exit_code,
                    latency_ms=ev.latency_ms,
                )
                self._cur_tool = None
            self._scroll_to_bottom()
            return

        if isinstance(ev, StatusChange):
            _freeze_assistant()
            self.query_one(StatusBar).set_state(ev.state)
            debug_panel.append(f"[{ev.state}]")
            return

        if isinstance(ev, MetricChunk):
            debug_panel.append(_fmt_metric(ev.metrics))
            return

        if isinstance(ev, FinalMessage):
            _freeze_assistant()
            if ev.metrics.get("total_tool_calls", 0):
                debug_panel.append(
                    f"final: {ev.metrics['total_tool_calls']} tools · "
                    f"{ev.metrics.get('total_latency_ms', 0)}ms"
                )
            return

        if isinstance(ev, Card):
            _freeze_assistant()
            history.mount(Static(json.dumps(ev.data, ensure_ascii=False, indent=2)))
            self._scroll_to_bottom()
            return

        if isinstance(ev, ErrorEvent):
            _freeze_assistant()
            history.mount(Static(f"error: {ev.code} — {ev.msg}", classes="error"))
            self._scroll_to_bottom()
            return

    def _scroll_to_bottom(self) -> None:
        try:
            self.query_one("#history").scroll_end(animate=False)
        except Exception:
            pass


# =================== Channel adapter ===================


class TextualChannel:
    """Channel 协议实现。Textual 版 full-screen TUI。

    Constructor 不启动 App；start() 才启动。stop() 才退出。
    """

    def __init__(
        self,
        session_key: str = "default",
        debug: bool = False,
        options: dict | None = None,
    ) -> None:
        self._session_key = session_key
        self._debug = debug
        self._options = options or {}

        self._out_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._in_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._stop = asyncio.Event()

        self._app: ChatApp | None = None
        self._run_task: asyncio.Task | None = None

    # ---- lifecycle ----

    async def start(self) -> None:
        self._app = ChatApp(channel=self)
        self._run_task = asyncio.create_task(self._app.run_async())

    async def stop(self) -> None:
        if self._app and not self._app._exited:
            self._stop.set()
            self._app.call_later(self._app.exit)
        if self._run_task:
            try:
                await asyncio.wait_for(self._run_task, timeout=2.0)
            except asyncio.TimeoutError:
                self._run_task.cancel()

    # ---- Channel 协议 ----

    async def listen(self) -> AsyncIterator[InboundEvent]:
        while not self._stop.is_set():
            try:
                yield await asyncio.wait_for(self._in_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

    async def send(self, event: StreamEvent) -> None:
        # non-blocking：Gateway pump_outbound 在等 send 返回
        self._out_q.put_nowait(event)

    # ---- App 调用 ----

    def submit_user_input(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.lower() in ("exit", "quit"):
            self._stop.set()
            if self._app and not self._app._exited:
                self._app.call_later(self._app.exit)
            return
        self._in_q.put_nowait(
            InboundEvent(session_key=self._session_key, kind="message", text=text)
        )


# =================== helpers ===================


def _fmt_args(args: dict) -> str:
    return " ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())


def _fmt_metric(m: dict) -> str:
    tokens = m.get("tokens") or {}
    parts = [
        f"step={m.get('step_idx')}",
        f"latency={m.get('latency_ms')}ms",
        f"tools={m.get('tool_calls_count', 0)}",
    ]
    if tokens:
        parts.append(f"in={tokens.get('prompt_tokens', '?')}")
        parts.append(f"out={tokens.get('completion_tokens', '?')}")
    return " ".join(parts)