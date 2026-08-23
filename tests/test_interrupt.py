"""Interrupt 真打断测试。

验证 MonoDesk 用户在 agent 长输出中途点「停止」时：
- 当前 react step 被取消（不再产生新 token）
- messages / step_idx / session_metric 回滚到 step 开始前
- 后续 user message 走新 react（无残留）

对比：之前 interrupt 帧只是被当作普通 user msg append（无 effect）。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from core.channel.base import Channel
from tests._inprocess_bridge import InProcessBridge as Gateway
from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.compression import CompressionService
from core.loop.engine import LoopEngine
from core.loop.tool_registry import ToolRegistry
from core.loop.tools import BashTool, ReadToolResultBudgetTool, SkillLoadTool, WaitIoTool
from core.memory import FsMemoryStore
from core.protocol import (
    FinalMessage,
    InboundEvent,
    LlmChunk,
    LLMProxy,
    StatusChange,
    StreamEvent,
    ToolEnd,
)
from core.sandbox import BashRunner
from core.skill_service import SkillService


class SlowLLM(LLMProxy):
    """第一步吐 30 个 token，每个 100ms（3s 完成）；第二步 finalize。"""

    def __init__(self) -> None:
        self._step = 0

    async def stream(self, messages, tools=None, options=None):
        step = self._step
        self._step += 1
        if step == 0:
            for i in range(30):
                await asyncio.sleep(0.1)
                yield LlmChunk(delta_text=f"t{i} ")
            yield LlmChunk(finish_reason="stop")
        else:
            yield LlmChunk(delta_text="second turn", finish_reason="stop")


class StubChannel(Channel):
    def __init__(self) -> None:
        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._sent: list[StreamEvent] = []

    async def start(self) -> None: pass
    async def stop(self) -> None: self._stop.set()

    async def listen(self):
        while not self._stop.is_set():
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue

    async def send(self, event: StreamEvent) -> None:
        self._sent.append(event)


def build(tmp: Path):
    ws = tmp / "ws"; ws.mkdir(parents=True, exist_ok=True)
    mem = tmp / "mem"; mem.mkdir(exist_ok=True)
    skills = tmp / "skills"; skills.mkdir(exist_ok=True)
    skill_service = SkillService(skills)
    tools = ToolRegistry([
        BashTool(BashRunner(), ws),
        SkillLoadTool(skill_service),
        WaitIoTool(),
        ReadToolResultBudgetTool(),
    ])
    mem_store = FsMemoryStore(mem)
    ck = JsonlCheckpointStore(mem / "default" / "checkpoint.jsonl")
    compression = CompressionService(
        budget_tool=tools.get("read_tool_result_budget"),
        llm=None,  # type: ignore[arg-type]
    )
    return tools, mem_store, ck, skill_service, compression


async def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.05) -> bool:
    """轮询等到 predicate 为真。"""
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


async def test_interrupt_cancels_step() -> None:
    """react 进行中发 interrupt：token stream 必须中止，下一轮 user 必须被接收。"""
    tmp = Path("/tmp/test_interrupt_basic")
    shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
    tools, mem, ck, ss, comp = build(tmp)
    loop = LoopEngine(
        session_key="default", system_prompt="t",
        llm=SlowLLM(), tools=tools, compression=comp,
        memory=mem, checkpoint=ck, skill_service=ss,
    )
    ch = StubChannel()
    iq: asyncio.Queue[InboundEvent] = asyncio.Queue()
    oq: asyncio.Queue[StreamEvent] = asyncio.Queue()
    gw = Gateway(ch, loop_input=iq, loop_output=oq)
    gw_task = asyncio.create_task(gw.run())
    loop_task = asyncio.create_task(loop.run(iq, oq))

    try:
        # 1) 第一轮
        await ch._queue.put(InboundEvent(
            session_key="default", kind="message", text="first turn"))
        assert await _wait_for(lambda: any(getattr(e, "text", "") == "t0 " for e in ch._sent), 3.0), \
            "LLM never produced first token"

        # 2) interrupt
        await ch._queue.put(InboundEvent(
            session_key="default", kind="interrupt", text=""))
        assert await _wait_for(
            lambda: any(isinstance(e, StatusChange) and e.state == "idle" for e in ch._sent),
            3.0,
        ), "idle status never appeared after interrupt"

        # 3) 计数：cancel 之后不应继续吐完剩余 ~29 个 token
        token_after_idle = sum(
            1 for e in ch._sent
            if hasattr(e, "text") and e.text.startswith("t")
        )
        # interrupt 在 t0 之后不久发出 → token 数应远小于 30
        assert token_after_idle < 15, f"stream continued after interrupt: {token_after_idle} tokens"

        # 4) 之后 user 应能起新 react（messages 不残留）
        # 给 pumper / _select 一点时间收尾
        await asyncio.sleep(0.2)
        await ch._queue.put(InboundEvent(
            session_key="default", kind="message", text="second turn"))
        await asyncio.sleep(0.5)
        assert await _wait_for(
            lambda: any(getattr(e, "text", "") == "second turn" for e in ch._sent),
            3.0,
        ), "second turn never reached LLM"
        # 等 final（第二轮 react 完成）
        await _wait_for(lambda: any(isinstance(e, FinalMessage) for e in ch._sent), 3.0)

        # messages：被打断的 step 不留 assistant；第二次 react 留 user + assistant
        user_count = sum(1 for m in loop._messages if m.get("role") == "user")
        assert user_count == 2, f"expected 2 user msgs, got {user_count}: {loop._messages}"

        print("test_interrupt_cancels_step PASSED ✓")
    finally:
        for t in (gw_task, loop_task):
            t.cancel()
            try: await t
            except: pass


async def test_interrupt_before_react_does_nothing_harmful() -> None:
    """idle 时收到 interrupt：不报错，user 仍能被 react。"""
    tmp = Path("/tmp/test_interrupt_idle")
    shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
    tools, mem, ck, ss, comp = build(tmp)

    class ShortLLM(LLMProxy):
        async def stream(self, messages, tools=None, options=None):
            yield LlmChunk(delta_text="ok", finish_reason="stop")

    loop = LoopEngine(
        session_key="default", system_prompt="t",
        llm=ShortLLM(), tools=tools, compression=comp,
        memory=mem, checkpoint=ck, skill_service=ss,
    )
    ch = StubChannel()
    iq: asyncio.Queue[InboundEvent] = asyncio.Queue()
    oq: asyncio.Queue[StreamEvent] = asyncio.Queue()
    gw = Gateway(ch, loop_input=iq, loop_output=oq)
    gw_task = asyncio.create_task(gw.run())
    loop_task = asyncio.create_task(loop.run(iq, oq))

    try:
        await ch._queue.put(InboundEvent(
            session_key="default", kind="interrupt", text=""))
        await asyncio.sleep(0.3)
        await ch._queue.put(InboundEvent(
            session_key="default", kind="message", text="hi"))
        assert await _wait_for(
            lambda: any(getattr(e, "text", "") == "ok" for e in ch._sent),
            3.0,
        ), "normal message lost after stray interrupt"
        # user + assistant = 2（interrupt 没污染 messages）
        assert len(loop._messages) == 2
        print("test_interrupt_before_react_does_nothing_harmful PASSED ✓")
    finally:
        for t in (gw_task, loop_task):
            t.cancel()
            try: await t
            except: pass