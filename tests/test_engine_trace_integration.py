"""LoopEngine 集成测试：用 mock LLMProxy 跑 1 turn，断言 collector 收到正确 span。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.compression import CompressionService
from core.loop.engine import LoopEngine
from core.loop.tool_registry import ToolRegistry
from core.memory import FsMemoryStore
from core.observability import JsonlTraceStore, TraceCollector
from core.protocol import InboundEvent, LlmChunk, LLMProxy


class _MockLLM(LLMProxy):
    """最小 mock：一次 stream 调用，yield 完整 text，无 tool_call。"""

    def __init__(self, model: str = "m1") -> None:
        self._model_name = model
        self.last_messages: list[dict[str, Any]] | None = None

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        self.last_messages = messages
        yield LlmChunk(delta_text="hello back")
        yield LlmChunk(
            finish_reason="stop",
            usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    @property
    def model(self) -> str:
        return self._model_name


class _MockLLMTool(LLMProxy):
    """一次 stream 调用：先 yield tool_call (wait_io)，finish_reason=tool_calls。"""

    def __init__(self) -> None:
        self.last_messages: list[dict[str, Any]] | None = None

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        self.last_messages = messages
        yield LlmChunk(
            delta_tool_calls=(
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "wait_io", "arguments": "{}"}},
            )
        )
        yield LlmChunk(
            finish_reason="tool_calls",
            usage={"prompt_tokens": 3, "completion_tokens": 1},
        )

    @property
    def model(self) -> str:
        return "mock-tool"


def _make_engine(
    tmp_path: Path, *, mock_llm: _MockLLM, traces: TraceCollector | None
) -> tuple[LoopEngine, asyncio.Queue, asyncio.Queue]:
    ck_path = tmp_path / "default" / "checkpoint.jsonl"
    ck_path.parent.mkdir(parents=True, exist_ok=True)
    ck = JsonlCheckpointStore(ck_path)

    mem = FsMemoryStore(tmp_path / "mem")
    compression = CompressionService(
        budget_tool=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
    )

    engine = LoopEngine(
        session_key="default",
        system_prompt="sys",
        llm=mock_llm,
        tools=ToolRegistry([]),
        compression=compression,
        memory=mem,
        checkpoint=ck,
        skill_summary="",
        max_steps=3,
        traces=traces,
    )
    return engine, asyncio.Queue(), asyncio.Queue()


def _msg(text: str) -> InboundEvent:
    return InboundEvent(session_key="default", kind="message", text=text, source="t")


def _interrupt() -> InboundEvent:
    return InboundEvent(session_key="default", kind="interrupt", text="", source="t")


async def _drain_until_final(out_q: asyncio.Queue) -> None:
    """把 out_q 抽干直到 FinalMessage 出现或超时（end_run 在 FinalMessage 之后）。"""
    from core.protocol import FinalMessage
    for _ in range(200):
        try:
            ev = await asyncio.wait_for(out_q.get(), timeout=0.5)
            if isinstance(ev, FinalMessage):
                return
        except asyncio.TimeoutError:
            return


async def _shutdown_engine(task: asyncio.Task) -> None:
    """final 之后 engine 进入 wait_io 阻塞在 sub_queue.get()；interrupt 路径无
    step_task 可 cancel，没法正常退出。直接 cancel task 即可。
    """
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def test_engine_records_llm_span(tmp_path: Path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")
    mock = _MockLLM("m1")
    engine, in_q, out_q = _make_engine(tmp_path, mock_llm=mock, traces=collector)

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    await _drain_until_final(out_q)
    # 额外让一次 loop tick 跑完（end_run 是 await 在 FinalMessage 之后）
    await asyncio.sleep(0.05)

    runs = await store.list_runs("default")
    assert len(runs) == 1
    full = await store.get_run("default", runs[0].run_id)
    assert full.status == "ok"
    assert full.final_text == "hello back"
    assert len(full.turns) >= 1

    reasoning = [
        s for t in full.turns for s in t.spans if s.kind.value == "reasoning"
    ]
    assert len(reasoning) >= 1
    sp = reasoning[0]
    assert sp.attributes["model"] == "m1"
    assert sp.attributes["response_text"] == "hello back"
    assert sp.attributes["usage"]["completion_tokens"] == 2
    assert sp.attributes["finish_reason"] == "stop"
    # messages 必须包含 system + user（user 现在是 XML event 包装）
    msgs = sp.attributes["messages"]
    assert any(m.get("role") == "system" for m in msgs)
    assert any(
        m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and '<event' in m["content"]
        and 'kind="user_input"' in m["content"]
        and "hi" in m["content"]
        for m in msgs
    )

    # 清理
    await _shutdown_engine(task)


async def test_engine_without_traces_works(tmp_path: Path):
    """traces=None 时不报错，FinalMessage.trace_id 为 None。"""
    mock = _MockLLM()
    engine, in_q, out_q = _make_engine(tmp_path, mock_llm=mock, traces=None)

    saw_final = False
    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))

    for _ in range(200):
        try:
            ev = await asyncio.wait_for(out_q.get(), timeout=0.5)
        except asyncio.TimeoutError:
            break
        from core.protocol import FinalMessage
        if isinstance(ev, FinalMessage):
            assert getattr(ev, "trace_id", None) is None
            saw_final = True
            break

    assert saw_final
    await _shutdown_engine(task)


async def test_status_and_final_carry_trace_id(tmp_path: Path):
    """status(thinking) / FinalMessage 应带 trace_id。"""
    from core.protocol import FinalMessage, StatusChange
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")
    mock = _MockLLM()
    engine, in_q, out_q = _make_engine(tmp_path, mock_llm=mock, traces=collector)

    saw_thinking = False
    saw_final_with_id = False

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    for _ in range(200):
        try:
            ev = await asyncio.wait_for(out_q.get(), timeout=0.5)
        except asyncio.TimeoutError:
            break
        if isinstance(ev, StatusChange) and ev.state == "thinking":
            assert ev.trace_id is not None
            saw_thinking = True
        if isinstance(ev, FinalMessage):
            assert ev.trace_id is not None
            saw_final_with_id = True
            break
    assert saw_thinking
    assert saw_final_with_id
    await _shutdown_engine(task)


async def test_metric_carry_trace_id_with_tool_call(tmp_path: Path):
    """有 tool_call 的 turn：StatusChange + MetricChunk 都带 trace_id。"""
    from core.loop import WaitIoTool
    from core.protocol import FinalMessage, MetricChunk, StatusChange
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")
    mock = _MockLLMTool()

    # 单独 build：注册 WaitIoTool 让 mock 的 tool_call 能落地
    ck_path = tmp_path / "default" / "checkpoint.jsonl"
    ck_path.parent.mkdir(parents=True, exist_ok=True)
    ck = JsonlCheckpointStore(ck_path)
    mem = FsMemoryStore(tmp_path / "mem")
    compression = CompressionService(
        budget_tool=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
    )
    engine = LoopEngine(
        session_key="default", system_prompt="sys", llm=mock,
        tools=ToolRegistry([WaitIoTool()]),
        compression=compression, memory=mem, checkpoint=ck,
        skill_summary="", max_steps=3, traces=collector,
    )
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()

    saw_metric = False
    saw_final = False

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    for _ in range(200):
        try:
            ev = await asyncio.wait_for(out_q.get(), timeout=0.5)
        except asyncio.TimeoutError:
            break
        if isinstance(ev, MetricChunk):
            assert ev.trace_id is not None
            saw_metric = True
        if isinstance(ev, FinalMessage):
            saw_final = True
            break
    assert saw_metric
    assert saw_final
    await _shutdown_engine(task)