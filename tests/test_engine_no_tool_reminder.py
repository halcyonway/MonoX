"""no-tool-call reminder 单元测试。

检测 reasoning-only turn（content 空 ∧ tool_call 空 ∧ reasoning 非空 ∧
finish_reason=stop）→ 注入 system note 让 LLM 重出，最多 3 次后放过去。
见 spec/requirements/no-tool-call-reminder.md。

不重复覆盖已有的正常路径测试（见 test_engine_trace_integration.py 的
test_engine_records_llm_span / test_engine_without_traces_works）。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.compression import CompressionService
from core.loop.engine import LoopEngine
from core.loop.tool_registry import ToolRegistry
from core.memory import FsMemoryStore
from core.protocol import FinalMessage, InboundEvent, LlmChunk, LLMProxy

_PATH_VARS = {
    "MONOX_HOME": "/tmp",
    "MONOX_WORKSPACE_DIR": "/tmp/ws",
    "MONOX_MEMORY_DIR": "/tmp/mem",
    "MONOX_SKILLS_DIR": "/tmp/skills",
    "MONOX_TMP_DIR": "/tmp/tmp",
}


class _SelfRecoverLLM(LLMProxy):
    """前 N 次 reasoning-only 触发 reminder；之后恢复 final text。"""

    def __init__(self, recover_after: int) -> None:
        self.call_count = 0
        self.recover_after = recover_after
        self.last_messages: list[dict[str, Any]] | None = None

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        self.call_count += 1
        self.last_messages = messages
        if self.call_count <= self.recover_after:
            yield LlmChunk(delta_reasoning=f"thinking step {self.call_count}")
            yield LlmChunk(
                finish_reason="stop",
                usage={"prompt_tokens": 1, "completion_tokens": 0},
            )
            return
        yield LlmChunk(delta_text="recovered answer")
        yield LlmChunk(
            finish_reason="stop",
            usage={"prompt_tokens": 1, "completion_tokens": 2},
        )

    @property
    def model(self) -> str:
        return "mock-self-recover"


class _AlwaysReasoningOnlyLLM(LLMProxy):
    """永远 reasoning-only —— 用来测 count 上限。"""

    def __init__(self) -> None:
        self.call_count = 0
        self.last_messages: list[dict[str, Any]] | None = None

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        self.call_count += 1
        self.last_messages = messages
        yield LlmChunk(delta_reasoning=f"thinking {self.call_count}")
        yield LlmChunk(
            finish_reason="stop",
            usage={"prompt_tokens": 1, "completion_tokens": 0},
        )

    @property
    def model(self) -> str:
        return "mock-reasoning-only"


class _NonStopFinishLLM(LLMProxy):
    """finish_reason='length' —— 不触发 reminder。"""

    def __init__(self) -> None:
        self.call_count = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        self.call_count += 1
        yield LlmChunk(delta_reasoning="thinking")
        yield LlmChunk(
            finish_reason="length",
            usage={"prompt_tokens": 1, "completion_tokens": 0},
        )

    @property
    def model(self) -> str:
        return "mock-length"


def _make_engine(
    tmp_path: Path, mock_llm: LLMProxy, *, max_steps: int = 10
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
        path_vars=_PATH_VARS,
        max_steps=max_steps,
        traces=None,
    )
    return engine, asyncio.Queue(), asyncio.Queue()


def _msg(text: str) -> InboundEvent:
    return InboundEvent(session_key="default", kind="message", text=text, source="t")


async def _drain_until_final(out_q: asyncio.Queue) -> FinalMessage | None:
    final: FinalMessage | None = None
    for _ in range(500):
        try:
            ev = await asyncio.wait_for(out_q.get(), timeout=0.5)
            if isinstance(ev, FinalMessage):
                final = ev
                return final
        except asyncio.TimeoutError:
            return final
    return final


async def _shutdown_engine(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def _system_note_messages(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        m for m in msgs
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and "[system note]" in m["content"]
    ]


async def test_no_tool_reminder_injects_system_note_once(tmp_path: Path):
    """第 1 次 reasoning-only → 触发 reminder；LLM 第 2 次能恢复。"""
    mock = _SelfRecoverLLM(recover_after=1)
    engine, in_q, out_q = _make_engine(tmp_path, mock, max_steps=5)

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    final = await _drain_until_final(out_q)
    assert final is not None
    assert final.text == "recovered answer"

    assert engine._no_tool_reminder_count == 1
    assert mock.call_count == 2
    # 第二轮 LLM 看到的 messages 里有 reminder system note
    assert mock.last_messages is not None
    reminders = _system_note_messages(mock.last_messages)
    assert len(reminders) == 1
    assert "no tool call" in reminders[0]["content"]

    await _shutdown_engine(task)


async def test_no_tool_reminder_count_caps_at_3(tmp_path: Path):
    """永远 reasoning-only → count 到 3 后放过去，不再 inject；走 final 路径输出空 final。"""
    mock = _AlwaysReasoningOnlyLLM()
    engine, in_q, out_q = _make_engine(tmp_path, mock, max_steps=10)

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    final = await _drain_until_final(out_q)
    assert final is not None

    # 4 次调用：3 次 reminder + 1 次「放过去」走空 final 路径
    assert engine._no_tool_reminder_count == 3
    assert mock.call_count == 4
    # 第 4 次 LLM 调用时 messages 里**累积**有 3 条 reminder（之前 3 次 inject 的）；
    # 第 4 次本身不再追加新 reminder（count 已到 3 走 fallback）
    assert mock.last_messages is not None
    reminders = _system_note_messages(mock.last_messages)
    assert len(reminders) == 3, (
        f"前 3 次 inject 累积 3 条 reminder，第 4 次不再追加，实际 {len(reminders)} 条"
    )

    await _shutdown_engine(task)


async def test_no_tool_reminder_injects_each_time_until_cap(tmp_path: Path):
    """每次 reasoning-only 都 inject；累计 3 条 reminder system note。"""
    mock = _SelfRecoverLLM(recover_after=3)  # 前 3 次 reasoning-only，第 4 次恢复
    engine, in_q, out_q = _make_engine(tmp_path, mock, max_steps=10)

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    final = await _drain_until_final(out_q)
    assert final is not None
    assert final.text == "recovered answer"

    # 3 次 reminder + 1 次恢复
    assert engine._no_tool_reminder_count == 3
    assert mock.call_count == 4
    # 第 4 次调用（恢复）前 messages 里累计有 3 条 reminder
    assert mock.last_messages is not None
    assert len(_system_note_messages(mock.last_messages)) == 3

    await _shutdown_engine(task)


async def test_no_tool_reminder_not_triggered_when_finish_reason_not_stop(tmp_path: Path):
    """finish_reason='length' 时即使 reasoning-only 也不触发 reminder。"""
    mock = _NonStopFinishLLM()
    engine, in_q, out_q = _make_engine(tmp_path, mock, max_steps=3)

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    final = await _drain_until_final(out_q)
    assert final is not None

    assert engine._no_tool_reminder_count == 0
    assert mock.call_count == 1  # 只调一次，不 retry

    await _shutdown_engine(task)


async def test_no_tool_reminder_does_not_persist_to_checkpoint(tmp_path: Path):
    """reminder system note 不写 checkpoint，避免污染 replay。"""
    mock = _SelfRecoverLLM(recover_after=1)
    engine, in_q, out_q = _make_engine(tmp_path, mock, max_steps=5)

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    await _drain_until_final(out_q)

    ck_path = tmp_path / "default" / "checkpoint.jsonl"
    assert ck_path.exists()
    lines = ck_path.read_text(encoding="utf-8").strip().split("\n")
    msgs = [json.loads(line) for line in lines if line.strip()]
    contents = [m.get("content", "") for m in msgs if m.get("kind") == "msg"]
    assert not any("[system note]" in str(c) for c in contents), (
        f"checkpoint 不该持久化 reminder system note, got: {contents}"
    )

    await _shutdown_engine(task)


async def test_no_tool_reminder_step_metric_recorded(tmp_path: Path):
    """reminder continue 路径必须把本 step 的 step_metric 加进 SessionMetric。"""
    mock = _SelfRecoverLLM(recover_after=1)
    engine, in_q, out_q = _make_engine(tmp_path, mock, max_steps=5)

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    final = await _drain_until_final(out_q)
    assert final is not None

    # final.metrics 是 SessionMetric.snapshot() 的 dict：{"steps", "total_latency_ms", "total_tool_calls"}
    assert final.metrics is not None
    # 2 次 step 都计入：第一次 reminder + 第二次恢复
    assert final.metrics["steps"] == 2
    # reminder 路径下 tool_calls_count=0（确实没调 tool），恢复路径也是 0（plain text）
    assert final.metrics["total_tool_calls"] == 0
    # total_latency_ms 累计两次
    assert final.metrics["total_latency_ms"] >= 0

    await _shutdown_engine(task)
