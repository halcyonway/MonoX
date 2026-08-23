"""SessionManager — Runtime 多 session 化的核心。

Runtime 端每个 `session_key` 一个独立 `LoopEngine` 实例：

- lazy create：`dispatch_inbound` 收到新 key 时才构造 + 从 `JsonlCheckpointStore` 恢复
- idle sweep：每 N 秒检查 `now - last_active_ts`，超阈值则 `destroy` + 从 dict 删除
- 每 session 一份 `JsonlCheckpointStore`（多 session 不能共享 jsonl 文件）
- `FsMemoryStore` / `LLMProxy` / `ToolRegistry` / `CompressionService` 共享（无 session 状态）
- clock seam：`time_fn` / `sweep_interval_sec` 可注入用于测试

SessionManager 与 RuntimeServer 协作：
- `_create` 时通过注入的 `outbound_register(session_key, output_q)` 回调让 RuntimeServer
  启动对应 session 的 outbound consumer
- `destroy` 时通过 `outbound_unregister(session_key)` 让 RuntimeServer 取消 consumer + 清 last_active
- SessionManager 自己持有 `dispatch_inbound` 作为 inbound handler 给 RuntimeServer 注册
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.compression import CompressionService
from core.loop.engine import LoopEngine
from core.loop.tool_registry import ToolRegistry
from core.memory import FsMemoryStore
from core.observability import JsonlTraceStore, TraceCollector
from core.protocol import InboundEvent, StreamEvent

_log = logging.getLogger("monox.session_manager")


@dataclass
class SessionLoop:
    session_key: str
    loop_engine: LoopEngine
    input_q: asyncio.Queue[InboundEvent]
    output_q: asyncio.Queue[StreamEvent]
    last_active_ts: float
    task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(
                self.loop_engine.run(self.input_q, self.output_q),
                name=f"loop:{self.session_key}",
            )

    async def destroy(self) -> None:
        if self.task is None:
            return
        task = self.task
        self.task = None
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# 注入的 RuntimeServer 协作回调（避免 SessionManager 直接 import RuntimeServer）
OutboundRegister = Callable[[str, asyncio.Queue[StreamEvent]], Awaitable[None]]
OutboundUnregister = Callable[[str], Awaitable[None]]


class SessionManager:
    IDLE_TIMEOUT_SEC = 300.0
    SWEEP_INTERVAL_SEC = 30.0

    def __init__(
        self,
        *,
        llm: Any,
        compression_llm: Any,
        tools: ToolRegistry,
        compression: CompressionService,
        memory: FsMemoryStore,
        memory_root: Path,
        system_prompt: str,
        skill_summary: str,
        max_steps: int = 30,
        outbound_register: OutboundRegister | None = None,
        outbound_unregister: OutboundUnregister | None = None,
        time_fn: Callable[[], float] = time.time,
        idle_timeout_sec: float = IDLE_TIMEOUT_SEC,
        sweep_interval_sec: float = SWEEP_INTERVAL_SEC,
        enable_traces: bool = True,
    ) -> None:
        self._llm = llm
        self._compression_llm = compression_llm
        self._tools = tools
        self._compression = compression
        self._memory = memory
        self._memory_root = memory_root
        self._system_prompt = system_prompt
        self._skill_summary = skill_summary
        self._max_steps = max_steps
        self._outbound_register = outbound_register
        self._outbound_unregister = outbound_unregister

        self._time_fn = time_fn
        self._idle_timeout_sec = idle_timeout_sec
        self._sweep_interval_sec = sweep_interval_sec
        # 可观测性：默认开 trace；core 改极少代码；测试可关
        self._enable_traces = enable_traces

        self._sessions: dict[str, SessionLoop] = {}
        self._stop = asyncio.Event()
        self._sweeper_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._sweeper_task is None:
            self._sweeper_task = asyncio.create_task(
                self._idle_sweeper(), name="session-sweeper"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sweeper_task = None
        # destroy 所有 active sessions
        for sk in list(self._sessions.keys()):
            await self._destroy_session(sk)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def dispatch_inbound(self, ev: InboundEvent) -> None:
        """RuntimeServer inbound handler：从 ws 收到 InboundEvent 后调用。

        - 新 session_key → lazy create（从 checkpoint 恢复）+ 启动 loop task
        - 已有 session_key → 直接 put 到 input_q
        - 更新 last_active_ts（idle 计时基准）
        """
        sl = self._sessions.get(ev.session_key)
        if sl is None:
            sl = await self._create(ev.session_key)
            self._sessions[ev.session_key] = sl
            sl.start()
        sl.last_active_ts = self._time_fn()
        await sl.input_q.put(ev)

    def active_sessions(self) -> list[str]:
        return sorted(
            sk for sk, sl in self._sessions.items() if sl.task is not None
        )

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def _create(self, session_key: str) -> SessionLoop:
        # 每 session_key 一份独立 JsonlCheckpointStore（多 session 不能共享 jsonl 文件）
        ck_path = self._memory_root / session_key / "checkpoint.jsonl"
        ck_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint = JsonlCheckpointStore(ck_path)

        # 可观测性：每个 session 一份独立的 JsonlTraceStore + TraceCollector
        trace_collector: TraceCollector | None = None
        if self._enable_traces:
            trace_store = JsonlTraceStore(
                self._memory_root / session_key / "traces.jsonl"
            )
            trace_collector = TraceCollector(trace_store, session_key)

        loop_engine = LoopEngine(
            session_key=session_key,
            system_prompt=self._system_prompt,
            llm=self._llm,
            tools=self._tools,
            compression=self._compression,
            memory=self._memory,
            checkpoint=checkpoint,
            skill_summary=self._skill_summary,
            max_steps=self._max_steps,
            traces=trace_collector,
        )

        sl = SessionLoop(
            session_key=session_key,
            loop_engine=loop_engine,
            input_q=asyncio.Queue(),
            output_q=asyncio.Queue(),
            last_active_ts=self._time_fn(),
            task=None,
        )
        # 注册到 RuntimeServer 的 outbound consumer（先于 start，确保下行有出口）
        if self._outbound_register is not None:
            await self._outbound_register(session_key, sl.output_q)
        return sl

    async def _destroy_session(self, session_key: str) -> None:
        sl = self._sessions.pop(session_key, None)
        if sl is None:
            return
        # 先 unregister RuntimeServer（停止 consumer 拉 output_q），避免 cancel 期间 send 失败
        if self._outbound_unregister is not None:
            await self._outbound_unregister(session_key)
        await sl.destroy()

    # ------------------------------------------------------------------
    # Idle sweep
    # ------------------------------------------------------------------

    async def _idle_sweeper(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._sweep_interval_sec
                )
                return  # stop set
            except asyncio.TimeoutError:
                pass
            now = self._time_fn()
            for sk, sl in list(self._sessions.items()):
                if (now - sl.last_active_ts) > self._idle_timeout_sec:
                    _log.info("session %r idle timeout → destroy", sk)
                    await self._destroy_session(sk)