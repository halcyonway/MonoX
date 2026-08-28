"""AsyncTask — 异步任务（subagent 是经典场景）。

agent 在 turn 中遇到「需要并行做 / 需要花很久做」的事情时，fork 一个后台任务继续
当前 turn；任务完成通过 notify_parent 投 InboundEvent 到父 session 的 input_q，
复用 engine 既有的「新事件唤醒」机制。设计见 spec/requirements/async-task.md。

关键决策（why）：
- child = 一个普通 SessionLoop（session_key = "async:<task_id>"），复用 checkpoint /
  tool registry / system prompt；AsyncTaskBridge 消费它的 output_q。
- 完成判据 = child 的第一个 FinalMessage。子 agent 契约（SUBAGENT_CONTRACT）保证它
  只在交付时结束 turn；违约中途 wait_io → completed + 半成品，接受该降级。
- cancel 走 interrupt 队列（不 task.cancel() SessionLoop.task——cancel 不跨任务传播，
  会留下孤儿 react step）。见 spec「Cancel / Timeout」节。
- task.json 在 start() 时即落盘（只等 mark_done 的话，running 中 crash 重启扫描不到）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from core.protocol import (
    Card,
    ErrorEvent,
    FinalMessage,
    InboundEvent,
    MetricChunk,
    ReasoningChunk,
    StatusChange,
    StreamEvent,
    TokenChunk,
    ToolEnd,
    ToolPending,
    ToolStart,
)
from core.protocol.wire_frames import FrameType, to_frame
from core.session_manager import SessionManager

_log = logging.getLogger("monox.async_task")

DEFAULT_TIMEOUT_SEC = 1800.0     # 30 min
MIN_TIMEOUT_SEC = 10.0
MAX_TIMEOUT_SEC = 7200.0         # 2h hard cap（防 agent 死循环占用资源）
EVENT_BUFFER_SIZE = 100          # per-task ring buffer
IDLE_WAIT_SEC = 5.0              # cancel 后等 child 回 idle 的兜底
SK_PREFIX = "async:"             # child session_key 前缀（run.py _register 也按它过滤）
PARENT_NOTIFY_TEXT_CAP = 8000    # 投给父 session 的 final_text 上限（防 context 膨胀）

AsyncTaskStatus = Literal[
    "pending", "running", "completed", "failed", "cancelled", "timed_out", "interrupted"
]

_TERMINAL: tuple[str, ...] = ("completed", "failed", "cancelled", "timed_out", "interrupted")

SUBAGENT_CONTRACT = """\
You are running as an autonomous async task (subagent). Contract:
- Work to completion in this single turn. Do NOT ask clarifying questions.
- Do NOT call wait_io mid-task. Your final message IS the deliverable — \
make it a self-contained summary of findings / changes.
- You have the full tool registry, including fork_task for nested subagents.
"""

_EVENT_KIND = {
    TokenChunk: "token",
    ReasoningChunk: "reasoning",
    ToolPending: "tool_pending",
    ToolStart: "tool_start",
    ToolEnd: "tool_end",
    StatusChange: "status",
    MetricChunk: "metric",
    Card: "card",
    ErrorEvent: "error",
}


@dataclass
class AsyncTask:
    task_id: str                 # "t_" + uuid4().hex[:12]，如 "t_4f9ea1b2c3d4"；UI 截短展示
    kind: str                    # "subagent" / "bash_long"
    description: str             # 第一句任务描述（UI / poll 用）
    parent_session_key: str      # 谁 fork 的（主 session_key）
    child_session_key: str       # "async:" + task_id（复用 SessionLoop）
    status: AsyncTaskStatus      # interrupted：Runtime 重启时对 running 的终态标记
    created_at: float
    command: str | None = None   # bash_long 的 shell 命令（subagent 为 None）
    meta: dict[str, Any] = field(default_factory=dict)  # 扩展 kv，agent fork 时填
    started_at: float | None = None    # SessionLoop 实际启动时间
    finished_at: float | None = None
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    final_text: str | None = None      # 子 agent 最后一句 final（completed 时填）
    error: str | None = None           # 失败原因
    cancel_reason: str | None = None   # agent / user / timeout / runtime_shutdown

    def copy(self) -> "AsyncTask":
        """副本（内部记录可变，不外借引用）。"""
        return replace(self, meta=dict(self.meta))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> dict[str, Any]:
        """wire 层 AsyncTaskSummary（与 MonoDesk protocol.ts 对齐）。"""
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "description": self.description,
            "status": self.status,
            "parent_session_key": self.parent_session_key,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timeout_sec": self.timeout_sec,
            "meta": self.meta,
            "final_text": self.final_text,
            "error": self.error,
        }


class AsyncTaskBridge:
    """消费 child SessionLoop 的 output_q：事件转发给 manager，终态触发 _on_child_done。

    FinalMessage → completed；ErrorEvent → failed；cancel 路径由 manager 直接收摊
    （bridge 收到 stop 信号退出，不负责标记状态）。
    """

    def __init__(
        self,
        *,
        task_id: str,
        output_q: asyncio.Queue[StreamEvent],
        on_event: Callable[[str, StreamEvent], Awaitable[None]],
        on_done: Callable[[str, FinalMessage | None, ErrorEvent | None], Awaitable[None]],
    ) -> None:
        self._task_id = task_id
        self._output_q = output_q
        self._on_event = on_event
        self._on_done = on_done
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name=f"async-task-bridge:{self._task_id}")

    def stop(self) -> None:
        """停 bridge（不取消 task——用哨兵让它从 get() 里自然退出）。"""
        self._output_q.put_nowait(None)  # type: ignore[arg-type]

    async def run(self) -> None:
        while True:
            ev = await self._output_q.get()
            if ev is None:
                return
            if isinstance(ev, FinalMessage):
                await self._on_done(self._task_id, ev, None)
                return
            if isinstance(ev, ErrorEvent):
                await self._on_done(self._task_id, None, ev)
                return
            await self._on_event(self._task_id, ev)


def _default_timer_factory(
    delay: float, callback: Callable[[], None], arg: Any
) -> object:
    return asyncio.get_running_loop().call_later(delay, callback, arg)


class AsyncTaskManager:
    """Runtime 进程内单例（run.py 持有，跟 SessionManager 同层）。

    on_event(ftype, data)：wire 出口——帧信封（v/seq/ts）由 RuntimeServer 统一加盖，
    这里只产出 data dict。ftype 取 FrameType.ASYNC_TASK_* 常量。
    """

    def __init__(
        self,
        *,
        session_manager: SessionManager,
        state_root: Path,
        on_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        time_fn: Callable[[], float] = time.time,
        default_timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        bash_cwd: Path | None = None,
        bridge_factory: Callable[[AsyncTask, asyncio.Queue[StreamEvent]], AsyncTaskBridge] | None = None,
        timer_factory: Callable[[float, Callable[[], None], Any], object] | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._state_root = state_root
        self._on_event = on_event
        self._time_fn = time_fn
        self._default_timeout_sec = default_timeout_sec
        self._bash_cwd = bash_cwd
        self._bridge_factory = bridge_factory or self._default_bridge
        self._timer_factory = timer_factory or _default_timer_factory

        self._tasks: dict[str, AsyncTask] = {}
        self._bridges: dict[str, AsyncTaskBridge] = {}
        self._timers: dict[str, object] = {}
        self._recent_events: dict[str, deque[dict[str, Any]]] = {}
        self._idle_events: dict[str, asyncio.Event] = {}
        # bash_long 的进程句柄 / asyncio task（cancel / shutdown 时 kill + 收尸）
        self._bash_procs: dict[str, asyncio.subprocess.Process] = {}
        self._bash_tasks: dict[str, asyncio.Task[None]] = {}
        # cancel 进行中的 task（重入保护；终态标记后置到 _finish，否则 bridge 会因
        # terminal 检查丢弃 StatusChange(idle)，idle 信号永远不 set）
        self._cancelling: set[str] = set()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        description: str,
        parent_session_key: str,
        meta: dict[str, Any] | None = None,
        kind: str = "subagent",
        command: str | None = None,
        timeout_sec: float | None = None,
    ) -> AsyncTask:
        """同步返回 AsyncTask（task_id 已生成；subagent child / bash 进程异步启动）。"""
        timeout = self._default_timeout_sec if timeout_sec is None else float(timeout_sec)
        if not (MIN_TIMEOUT_SEC <= timeout <= MAX_TIMEOUT_SEC):
            raise ValueError(
                f"timeout_sec must be within [{MIN_TIMEOUT_SEC:.0f}, {MAX_TIMEOUT_SEC:.0f}]"
            )
        if kind not in ("subagent", "bash_long"):
            raise ValueError(f"unknown kind: {kind!r}")
        if kind == "bash_long" and not (command or "").strip():
            raise ValueError("command is required for kind=bash_long")

        task_id = "t_" + uuid.uuid4().hex[:12]
        task = AsyncTask(
            task_id=task_id,
            kind=kind,
            description=description,
            parent_session_key=parent_session_key,
            child_session_key=SK_PREFIX + task_id,
            status="running",
            created_at=self._time_fn(),
            command=command,
            meta=dict(meta or {}),
            timeout_sec=timeout,
        )
        self._tasks[task_id] = task
        self._recent_events[task_id] = deque(maxlen=EVENT_BUFFER_SIZE)
        self._persist(task)

        if kind == "bash_long":
            self._start_bash(task)
        else:
            # 首条消息 = 子 agent 契约 + 任务描述；event_type 进 XML attr，LLM 可见
            await self._session_manager.dispatch_inbound(InboundEvent(
                session_key=task.child_session_key,
                kind="message",
                text=f"{SUBAGENT_CONTRACT}\nTask: {description}",
                source="async_task",
                event_type="async-task-prompt",
                timestamp=self._time_fn(),
                meta={
                    "parent_session_key": parent_session_key,
                    "task_id": task_id,
                    "kind": kind,
                },
            ))
            sl = self._session_manager.get_loop(task.child_session_key)
            if sl is not None:
                task.started_at = self._time_fn()
            # 桥接 output_q → ring buffer + wire fan-out
            bridge = self._bridge_factory(task, sl.output_q if sl is not None else asyncio.Queue())
            self._bridges[task_id] = bridge
            bridge.start()

        # timeout 兜底
        self._timers[task_id] = self._timer_factory(timeout, self._on_timer, task_id)

        await self._emit(FrameType.ASYNC_TASK_CREATED, {
            "session_key": task.parent_session_key,
            "task_id": task.task_id,
            "kind": task.kind,
            "description": task.description,
            "meta": task.meta,
            "parent_session_key": task.parent_session_key,
            "timeout_sec": task.timeout_sec,
            "created_at": task.created_at,
        })
        return task.copy()

    # ------------------------------------------------------------------
    # bash_long：后台 shell 命令（无 SessionLoop，进程句柄在 manager 手里）
    # ------------------------------------------------------------------

    def _start_bash(self, task: AsyncTask) -> None:
        """起 bash 子进程（独立进程组，cancel / timeout 可整组 kill）。

        同步启动进程 + create_task 消费；start() 立即返回 task_id。
        """
        task.started_at = self._time_fn()

        async def _run() -> None:
            proc = await asyncio.create_subprocess_shell(
                task.command or "",
                cwd=str(self._bash_cwd) if self._bash_cwd else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,  # 独立进程组：killpg 连 shell 的子进程一起杀
            )
            self._bash_procs[task.task_id] = proc
            try:
                stdout_b, stderr_b = await proc.communicate()
            except asyncio.CancelledError:
                return  # cancel / shutdown 路径负责收尾
            if task.task_id in self._cancelling or task.status in _TERMINAL:
                return  # kill 产生的退出交给 cancel 路径标记终态
            out = stdout_b.decode(errors="replace")
            err = stderr_b.decode(errors="replace")
            # 输出喂 ring buffer + wire（MonoDesk 详情页 / terminal 折叠行有内容可看）
            if out:
                await self._on_child_event(task.task_id, TokenChunk(text=out[-8000:]))
            if err:
                await self._on_child_event(task.task_id, TokenChunk(text=f"\n[stderr]\n{err[-2000:]}"))
            task.final_text = (out + (f"\n[stderr]\n{err}" if err.strip() else "")).strip()[:PARENT_NOTIFY_TEXT_CAP] or None
            if proc.returncode == 0:
                await self._finish(task, status="completed")
            else:
                task.error = f"exit code {proc.returncode}"
                await self._finish(task, status="failed")

        self._bash_tasks[task.task_id] = asyncio.create_task(
            _run(), name=f"async-task-bash:{task.task_id}"
        )

    async def _cancel_bash(self, task: AsyncTask) -> None:
        """kill 进程组 → 等 _run_bash 收尾 → 统一走 _finish（cancel/timeout 共用）。"""
        proc = self._bash_procs.get(task.task_id)
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        bash_task = self._bash_tasks.pop(task.task_id, None)
        if bash_task is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(bash_task, timeout=IDLE_WAIT_SEC)
            if not bash_task.done():
                bash_task.cancel()
        self._bash_procs.pop(task.task_id, None)

    async def cancel(self, task_id: str, *, reason: str) -> bool:
        """三种入口统一出口：agent cancel_task / MonoDesk 按钮 / timeout。

        投 interrupt 进 child input_q（复用 engine 协作中断：回滚 + idle），等 idle 后
        destroy session。终态标记放在收尾（_finish）——提前置终态会让 bridge 的
        terminal 检查丢掉 idle 信号。找不到 task / 已终态 / 正在 cancel → False（容错）。
        """
        task = self._tasks.get(task_id)
        _log.info("[cancel] task_id=%s reason=%s task=%s status=%s cancelling=%s",
                  task_id, reason, task.task_id if task else None,
                  task.status if task else None, task_id in self._cancelling)
        if task is None or task.status in _TERMINAL or task_id in self._cancelling:
            return False
        self._cancelling.add(task_id)
        status: AsyncTaskStatus = "timed_out" if reason == "timeout" else "cancelled"
        task.cancel_reason = reason
        try:
            if task.kind == "bash_long":
                await self._cancel_bash(task)
            else:
                sl = self._session_manager.get_loop(task.child_session_key)
                _log.info("[cancel] got sl=%s for child_sk=%s", sl is not None, task.child_session_key)
                if sl is not None:
                    idle = asyncio.Event()
                    self._idle_events[task_id] = idle
                    _log.info("[cancel] putting interrupt to child input_q child_sk=%s", task.child_session_key)
                    sl.input_q.put_nowait(InboundEvent(
                        session_key=task.child_session_key,
                        kind="interrupt",
                        text="",
                        source="async_task",
                        event_type="async-task-cancel",
                        timestamp=self._time_fn(),
                    ))
                    _log.info("[cancel] interrupt queued, waiting for idle child_sk=%s", task.child_session_key)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(idle.wait(), timeout=IDLE_WAIT_SEC)
                    _log.info("[cancel] idle wait done or timed out child_sk=%s", task.child_session_key)
                    self._idle_events.pop(task_id, None)
                    _log.info("[cancel] destroying session child_sk=%s", task.child_session_key)
                    await self._session_manager.destroy_session(task.child_session_key)
                    _log.info("[cancel] session destroyed child_sk=%s", task.child_session_key)

                bridge = self._bridges.pop(task_id, None)
                if bridge is not None:
                    bridge.stop()
        finally:
            self._cancelling.discard(task_id)
        _log.info("[cancel] calling _finish task_id=%s status=%s", task_id, status)
        await self._finish(task, status=status)
        _log.info("[cancel] _finish done task_id=%s", task_id)
        return True

    def get(self, task_id: str) -> AsyncTask | None:
        """返回副本——内部记录可变，不外借引用。"""
        task = self._tasks.get(task_id)
        return task.copy() if task is not None else None

    def list(
        self,
        *,
        parent_session_key: str | None = None,
        status: list[str] | None = None,
    ) -> list[AsyncTask]:
        statuses = set(status) if status else None
        return [
            t.copy() for t in self._tasks.values()
            if (parent_session_key is None or t.parent_session_key == parent_session_key)
            and (statuses is None or t.status in statuses)
        ]

    def snapshot(self, task_id: str) -> tuple[AsyncTask, list[dict[str, Any]]] | None:
        """详情 + 最近 N 条 event。"""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        return task.copy(), list(self._recent_events.get(task_id, ()))

    async def emit_list(
        self,
        *,
        session_key: str = "",
        status: list[str] | None = None,
    ) -> None:
        """async_task_list_query 响应：全量任务列表（全局 UI 状态，fan-out 给所有订阅者）。"""
        statuses = set(status) if status else None
        tasks = [
            t.summary() for t in self._tasks.values()
            if statuses is None or t.status in statuses
        ]
        await self._emit(FrameType.ASYNC_TASK_LIST, {
            "session_key": session_key,
            "tasks": tasks,
        })

    def load_from_disk(self) -> None:
        """启动时扫描 task.json 重建索引；status=running 的改 interrupted（child loop 不复存在）。"""
        for path in sorted(self._state_root.glob(f"{SK_PREFIX}*/task.json")):
            try:
                data = json.loads(path.read_text())
                task = AsyncTask(
                    **{k: v for k, v in data.items() if k in AsyncTask.__dataclass_fields__}
                )
            except (json.JSONDecodeError, OSError, TypeError) as exc:
                _log.warning("skip bad task.json %s: %s", path, exc)
                continue
            if task.status == "running":
                task.status = "interrupted"
                task.finished_at = self._time_fn()
            self._tasks[task.task_id] = task
            self._recent_events[task.task_id] = deque(maxlen=EVENT_BUFFER_SIZE)
            if task.status == "interrupted":
                self._persist(task)
        if self._tasks:
            _log.info("restored %d async task(s) from %s", len(self._tasks), self._state_root)

    async def shutdown(self) -> None:
        """Runtime 退出时收摊：cancel 所有 running task 的 timer / bridge / bash 进程，flush task.json。

        session 本体由 run.py 随后的 session_manager.stop() 统一销毁。
        """
        for handle in self._timers.values():
            with contextlib.suppress(AttributeError, TypeError):
                handle.cancel()  # type: ignore[attr-defined]
        self._timers.clear()
        for task in list(self._tasks.values()):
            if task.status in ("pending", "running"):
                task.status = "cancelled"
                task.cancel_reason = "runtime_shutdown"
                task.finished_at = self._time_fn()
                if task.kind == "bash_long":
                    self._cancelling.add(task.task_id)
                    with contextlib.suppress(Exception):
                        await self._cancel_bash(task)
                    self._cancelling.discard(task.task_id)
                bridge = self._bridges.pop(task.task_id, None)
                if bridge is not None:
                    bridge.stop()
                self._persist(task)

    # ------------------------------------------------------------------
    # bridge 回调
    # ------------------------------------------------------------------

    async def _on_child_event(self, task_id: str, ev: StreamEvent) -> None:
        task = self._tasks.get(task_id)
        if task is None or task.status in _TERMINAL:
            return
        record = {"kind": _EVENT_KIND.get(type(ev), type(ev).__name__), "ts": self._time_fn()}
        with contextlib.suppress(Exception):
            record.update(asdict(ev))
        self._recent_events[task_id].append(record)

        if isinstance(ev, StatusChange) and ev.state == "idle":
            waiter = self._idle_events.get(task_id)
            if waiter is not None:
                waiter.set()

        inner = to_frame(ev, session_key=task.child_session_key)
        if inner is None:
            return
        await self._emit(FrameType.ASYNC_TASK_EVENT, {
            "session_key": task.parent_session_key,
            "task_id": task_id,
            "event": {"type": inner["type"], "data": inner["data"]},
        })

    async def _on_child_done(
        self, task_id: str, final: FinalMessage | None, error: ErrorEvent | None
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None or task.status in _TERMINAL:
            return
        if task_id in self._cancelling:
            return  # cancel 正在进行：状态迁移归 cancel（_finish）管
        self._bridges.pop(task_id, None)  # bridge 自己即将退出
        if final is not None:
            task.final_text = final.text
            await self._finish(task, status="completed")
        else:
            task.error = f"{error.code}: {error.msg}" if error else "unknown error"
            await self._finish(task, status="failed")
        # child 引擎已回 idle 停车，直接收摊（不等 idle sweep）
        with contextlib.suppress(Exception):
            await self._session_manager.destroy_session(task.child_session_key)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _default_bridge(self, task: AsyncTask, output_q: asyncio.Queue[StreamEvent]) -> AsyncTaskBridge:
        return AsyncTaskBridge(
            task_id=task.task_id,
            output_q=output_q,
            on_event=self._on_child_event,
            on_done=self._on_child_done,
        )

    def _on_timer(self, task_id: str) -> None:
        """TimerHandle 触发：cancel(task_id, reason='timeout')。"""
        task = self._tasks.get(task_id)
        if task is None or task.status in _TERMINAL:
            return
        try:
            asyncio.get_running_loop().create_task(self.cancel(task_id, reason="timeout"))
        except RuntimeError:
            _log.warning("timeout for %s fired but loop is closed", task_id)

    async def _finish(self, task: AsyncTask, *, status: AsyncTaskStatus) -> None:
        """终态收尾：状态 + 落盘 + timer 清理 + notify_parent + status 帧。"""
        if task.status in _TERMINAL and task.finished_at is not None:
            return
        task.status = status
        task.finished_at = self._time_fn()
        handle = self._timers.pop(task.task_id, None)
        if handle is not None:
            with contextlib.suppress(AttributeError, TypeError):
                handle.cancel()  # type: ignore[attr-defined]
        self._persist(task)

        if status == "completed":
            summary = f"completed in {self._duration(task):.0f}s"
            if task.final_text:
                text = task.final_text
                if len(text) > PARENT_NOTIFY_TEXT_CAP:
                    text = text[:PARENT_NOTIFY_TEXT_CAP] + "\n…[truncated]"
                summary = f"Async task {task.task_id} ({task.kind}) {summary}.\n\nResult:\n{text}"
        elif status == "failed":
            summary = f"Async task {task.task_id} ({task.kind}) failed: {task.error}"
        else:
            summary = f"Async task {task.task_id} ({task.kind}) was cancelled (reason: {task.cancel_reason})."

        # 结果投父 input_q → 复用 engine「新事件唤醒」；kind="system"（runtime
        # 内部通知，非用户输入），业务语义靠 event_type + XML attrs 区分
        try:
            await self._session_manager.dispatch_inbound(InboundEvent(
                session_key=task.parent_session_key,
                kind="system",
                text=summary,
                source="async_task",
                event_type="async-task-result",
                timestamp=self._time_fn(),
                meta={"task_id": task.task_id, "status": status, "kind": task.kind},
            ))
        except Exception:
            _log.exception("notify parent %s for task %s failed", task.parent_session_key, task.task_id)

        await self._emit(FrameType.ASYNC_TASK_STATUS, {
            "session_key": task.parent_session_key,
            "task_id": task.task_id,
            "status": status,
            "finished_at": task.finished_at,
            "duration_sec": self._duration(task),
            "final_text": task.final_text if status == "completed" else None,
            "error": task.error,
            "cancel_reason": task.cancel_reason,
        })

    def _duration(self, task: AsyncTask) -> float:
        if task.started_at is None or task.finished_at is None:
            return 0.0
        return max(0.0, task.finished_at - task.started_at)

    def _persist(self, task: AsyncTask) -> None:
        """task.json 落盘（<state_root>/async:<task_id>/task.json）。start + 每次状态迁移都写。"""
        path = self._state_root / task.child_session_key / "task.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(task.to_dict(), ensure_ascii=False, indent=2))
        except OSError as exc:
            _log.warning("persist task %s failed: %s", task.task_id, exc)

    async def _emit(self, ftype: str, data: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(ftype, data)
        except Exception as exc:
            _log.warning("async task wire emit failed (%s): %s", ftype, exc)
