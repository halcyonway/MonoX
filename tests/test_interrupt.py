"""interrupt.md 验收测试：双队列分流 + 三检查点 + 哨兵收敛。

覆盖 spec/REQUIREMENTS/interrupt.md「验证」节的六个场景。
直接驱动 LoopEngine.run(input_q, out_q)，不经 Gateway / channel adapter。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.compression import CompressionService
from core.loop.engine import LoopEngine
from core.loop.tool_registry import ToolRegistry
from core.memory import FsMemoryStore
from core.protocol import (
    FinalMessage,
    InboundEvent,
    LlmChunk,
    LLMProxy,
    StatusChange,
    StreamEvent,
)


class _StreamingLLM(LLMProxy):
    """多轮流式 mock：chunk 间让出控制权，给 C3 检查点命中机会。"""

    def __init__(self, call_texts: list[str]) -> None:
        self.call_texts = call_texts
        self.calls = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        idx = min(self.calls, len(self.call_texts) - 1)
        self.calls += 1
        for ch in self.call_texts[idx]:
            yield LlmChunk(delta_text=ch)
            await asyncio.sleep(0.02)
        yield LlmChunk(finish_reason="stop")


def _msg(text: str) -> InboundEvent:
    return InboundEvent(session_key="default", kind="message", text=text, source="t")


def _intr() -> InboundEvent:
    return InboundEvent(session_key="default", kind="interrupt", text="", source="t")


def _drain_output(out_q: asyncio.Queue[StreamEvent]) -> list[StreamEvent]:
    out: list[StreamEvent] = []
    while True:
        try:
            out.append(out_q.get_nowait())
        except asyncio.QueueEmpty:
            return out


async def _pump_until(out_q: asyncio.Queue[StreamEvent], pred, timeout: float = 3.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    events: list[StreamEvent] = []
    while loop.time() < deadline:
        try:
            ev = await asyncio.wait_for(out_q.get(), timeout=0.1)
        except asyncio.TimeoutError:
            continue
        events.append(ev)
        if pred(events):
            return True
    return False


def make_env(tmp_path: Path, llm: LLMProxy):
    ckpt = JsonlCheckpointStore(tmp_path / "default" / "checkpoint.jsonl")
    engine = LoopEngine(
        session_key="default",
        system_prompt="sys",
        llm=llm,
        tools=ToolRegistry([]),
        compression=CompressionService(budget_tool=None, llm=None),  # type: ignore[arg-type]
        memory=FsMemoryStore(tmp_path / "mem"),
        checkpoint=ckpt,
        max_steps=5,
        path_vars={"MONOX_MEMORY_DIR": str(tmp_path / "mem")},
    )
    in_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
    out_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
    return engine, in_q, out_q, ckpt


async def run_engine(engine: LoopEngine, in_q: asyncio.Queue, out_q: asyncio.Queue):
    task = asyncio.create_task(engine.run(in_q, out_q))
    await asyncio.sleep(0.01)
    return task


async def kill(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


_THINKING = lambda evs: any(isinstance(e, StatusChange) and e.state == "thinking" for e in evs)
_IDLE = lambda evs: any(isinstance(e, StatusChange) and e.state == "idle" for e in evs)


async def test_interrupt_during_stream(tmp_path):
    engine, in_q, out_q, _ckpt = make_env(tmp_path, _StreamingLLM(["hello world"]))
    task = await run_engine(engine, in_q, out_q)

    in_q.put_nowait(_msg("hi"))
    assert await _pump_until(out_q, _THINKING), "react 未启动"

    in_q.put_nowait(_intr())
    assert await _pump_until(out_q, _IDLE), "中断后应回到 idle"
    assert not any(isinstance(e, FinalMessage) for e in _drain_output(out_q)), \
        "中断不得产出 FinalMessage"

    roles = [m.get("role") for m in engine._messages]
    assert roles == ["user"], f"回滚失败：{roles}"

    await kill(task)


async def test_interrupt_at_step_start_not_polluting_context(tmp_path):
    llm = _StreamingLLM(["answer"])
    engine, in_q, out_q, ckpt = make_env(tmp_path, llm)
    task = await run_engine(engine, in_q, out_q)

    in_q.put_nowait(_msg("q"))
    in_q.put_nowait(_intr())
    await _pump_until(out_q, _IDLE)

    msgs = await ckpt.load_messages("default")
    for m in msgs:
        content = m.get("content") or ""
        assert 'kind="interrupt"' not in content, f"interrupt 泄漏进上下文: {content!r}"

    await kill(task)


async def test_repeated_interrupts_collapse(tmp_path):
    llm = _StreamingLLM(["long answer here", "ok"])
    engine, in_q, out_q, _ckpt = make_env(tmp_path, llm)
    task = await run_engine(engine, in_q, out_q)

    in_q.put_nowait(_msg("go"))
    assert await _pump_until(out_q, _THINKING)

    for _ in range(3):
        in_q.put_nowait(_intr())

    ok = await _pump_until(
        out_q,
        lambda evs: sum(1 for e in evs if isinstance(e, StatusChange) and e.state == "idle") >= 1,
    )
    assert ok
    await asyncio.sleep(0.15)
    _drain_output(out_q)

    # 核心断言：连发 interrupt 后引擎必须存活且可用
    in_q.put_nowait(_msg("after"))
    finals: list[FinalMessage] = []
    ok = await _pump_until(
        out_q,
        lambda evs: (finals.extend(e for e in evs if isinstance(e, FinalMessage)) or bool(finals)),
        timeout=5.0,
    )
    assert ok and finals[-1].text == "ok", "连发 interrupt 后引擎应保持可用"

    await kill(task)


async def test_message_after_interrupt_starts_new_turn(tmp_path):
    llm = _StreamingLLM(["first-reply", "second-reply"])
    engine, in_q, out_q, _ckpt = make_env(tmp_path, llm)
    task = await run_engine(engine, in_q, out_q)

    in_q.put_nowait(_msg("one"))
    assert await _pump_until(out_q, _THINKING)
    in_q.put_nowait(_intr())
    assert await _pump_until(out_q, _IDLE)

    in_q.put_nowait(_msg("two"))
    finals: list[FinalMessage] = []

    def grab_final(evs):
        finals.extend(e for e in evs if isinstance(e, FinalMessage))
        return bool(finals)

    assert await _pump_until(out_q, grab_final, timeout=5.0), \
        "新 message 应回到正常运行并产出 FinalMessage"
    assert finals[-1].text == "second-reply"
    assert llm.calls == 2

    await kill(task)


async def test_idle_interrupt_is_harmless(tmp_path):
    llm = _StreamingLLM(["ok"])
    engine, in_q, out_q, _ckpt = make_env(tmp_path, llm)
    task = await run_engine(engine, in_q, out_q)

    in_q.put_nowait(_intr())
    assert await _pump_until(out_q, _IDLE)
    assert not any(isinstance(e, FinalMessage) for e in _drain_output(out_q))

    in_q.put_nowait(_msg("later"))
    finals: list[FinalMessage] = []
    ok = await _pump_until(
        out_q,
        lambda evs: (finals.extend(e for e in evs if isinstance(e, FinalMessage)) or bool(finals)),
        timeout=5.0,
    )
    assert ok and finals[-1].text == "ok"

    await kill(task)


class _HangTool:
    name = "bash"
    schema = {
        "type": "function",
        "function": {
            "name": "bash",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        },
    }

    async def execute(self, call_id: str, arguments: dict) -> Any:
        from core.protocol import ToolResult

        await asyncio.sleep(30)
        return ToolResult(call_id=call_id, status="ok", stdout="", stderr="", exit_code=0)


class _ToolCallThenTextLLM(LLMProxy):
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages, tools=None, options=None) -> AsyncIterator[LlmChunk]:
        self.calls += 1
        if self.calls == 1:
            yield LlmChunk(
                delta_tool_calls=(
                    {"index": 0, "id": "c1", "type": "function",
                     "function": {"name": "bash", "arguments": "{}"}},
                ),
                finish_reason="tool_calls",
            )
        else:
            yield LlmChunk(delta_text="after-tool")
            yield LlmChunk(finish_reason="stop")


async def test_interrupt_during_tool_execution(tmp_path):
    llm = _ToolCallThenTextLLM()
    ck_dir = tmp_path / "default"
    ck_dir.mkdir(parents=True, exist_ok=True)
    engine = LoopEngine(
        session_key="default",
        system_prompt="sys",
        llm=llm,
        tools=ToolRegistry([_HangTool()]),
        compression=CompressionService(budget_tool=None, llm=None),  # type: ignore[arg-type]
        memory=FsMemoryStore(tmp_path / "mem"),
        checkpoint=JsonlCheckpointStore(ck_dir / "checkpoint.jsonl"),
        max_steps=5,
        path_vars={"MONOX_MEMORY_DIR": str(tmp_path / "mem")},
    )
    in_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
    out_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
    task = await run_engine(engine, in_q, out_q)

    in_q.put_nowait(_msg("run it"))
    saw_tool = await _pump_until(
        out_q,
        lambda evs: any(type(e).__name__ in ("ToolStart", "ToolEnd") for e in evs),
        timeout=5.0,
    )
    assert saw_tool, "未进入 tool 执行"

    t0 = asyncio.get_event_loop().time()
    in_q.put_nowait(_intr())
    ok = await _pump_until(out_q, _IDLE, timeout=7.0)
    elapsed = asyncio.get_event_loop().time() - t0

    assert ok, "tool 执行中的 interrupt 应经由 cancel 路径打断"
    assert elapsed < 6.5, f"应远小于 tool 的 30s 睡眠，实际 {elapsed:.1f}s"
    assert not any(isinstance(e, FinalMessage) for e in _drain_output(out_q))

    await kill(task)
