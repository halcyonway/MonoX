"""ReAct 主循环 + 状态机。

react 结束条件（满足任一即退出）：
1. agent 主动调用 wait_io tool → 等待外部输入
2. agent 完成（final message）且 input_queue 无新事件 → 等待外部输入
3. 达到 max_steps

agent 完成但有新的用户消息 → aggregate 进 context，继续 react（不退出）。

input_queue 始终由 Gateway 写入；engine 内部 drain 不到东西 = 没新事件。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from core.loop.compression import CompressionService
from core.loop.context import assemble_messages, format_tool_message
from core.loop.metric import SessionMetric, StepMetric
from core.loop.tool_registry import ToolRegistry
from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
from core.protocol import (
    CheckpointRecord,
    CheckpointStore,
    FinalMessage,
    InboundEvent,
    LLMProxy,
    MemoryStore,
    MetricChunk,
    ReasoningChunk,
    StatusChange,
    StreamEvent,
    TokenChunk,
    ToolEnd,
    ToolResult,
    ToolStart,
)


WAIT_IO_NAME = "wait_io"


class LoopEngine:
    def __init__(
        self,
        *,
        session_key: str,
        system_prompt: str,
        llm: LLMProxy,
        tools: ToolRegistry,
        compression: CompressionService,
        memory: MemoryStore,
        checkpoint: CheckpointStore,
        skill_summary: str,
        max_steps: int = 30,
    ) -> None:
        self._session_key = session_key
        self._system = system_prompt
        self._llm = llm
        self._tools = tools
        self._compression = compression
        self._memory = memory
        self._checkpoint = checkpoint
        self._skill_summary = skill_summary
        self._max_steps = max_steps

        self._messages: list[dict[str, Any]] = []
        self._step_idx = 0
        self._session_metric = SessionMetric()

    async def run(
        self,
        input_queue: asyncio.Queue[InboundEvent],
        output_queue: asyncio.Queue[StreamEvent],
    ) -> None:
        await self._restore()

        while True:
            ev = await input_queue.get()
            self._messages.append({"role": "user", "content": ev.text})

            final_text = await self._react(input_queue, output_queue)

            await output_queue.put(
                FinalMessage(text=final_text, metrics=self._session_metric.snapshot())
            )

    async def _restore(self) -> None:
        ck = await self._checkpoint.load_latest(self._session_key)
        if ck is None:
            return
        self._messages = list(ck.messages)
        self._step_idx = ck.step_idx + 1

    async def _react(
        self,
        input_queue: asyncio.Queue[InboundEvent],
        output_queue: asyncio.Queue[StreamEvent],
    ) -> str:
        final_text = ""

        for _ in range(self._max_steps):
            self._step_idx += 1
            step_metric = StepMetric(step_idx=self._step_idx)
            t0 = time.monotonic()

            # 1) drain input_queue → aggregate 新 user 消息
            for ev in _drain(input_queue):
                self._messages.append({"role": "user", "content": ev.text})

            # 2) L2 压缩（若需要）→ L3 落 Memory → assemble + LLM stream
            if self._compression.should_compress(self._messages):
                await output_queue.put(StatusChange(state="compressing"))

            self._messages = await self._compression.maybe_summarize(
                self._messages, self._session_key
            )

            # read_index 必须在 L2 之后，才能拿到刚写入 Memory 的 summary
            memory_index = await self._memory.read_index(self._session_key)
            messages = assemble_messages(
                self._system, memory_index, self._skill_summary, self._messages
            )
            tool_schemas = self._tools.schemas()

            full_text = ""
            tool_calls: list[dict[str, Any]] = []
            finish_reason: str | None = None
            usage: dict | None = None

            await output_queue.put(StatusChange(state="thinking"))
            async for chunk in self._llm.stream(messages, tools=tool_schemas):
                if chunk.delta_text:
                    full_text += chunk.delta_text
                    await output_queue.put(TokenChunk(text=chunk.delta_text))
                if chunk.delta_reasoning:
                    await output_queue.put(ReasoningChunk(text=chunk.delta_reasoning))
                if chunk.delta_tool_calls:
                    _merge_tool_calls(tool_calls, chunk.delta_tool_calls)
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
                if chunk.usage:
                    usage = chunk.usage

            step_metric.latency_ms = int((time.monotonic() - t0) * 1000)
            step_metric.tokens = usage

            # 3) final message 分支（agent 没调 tool 或 finish_reason=stop）
            if not tool_calls or finish_reason == "stop":
                self._messages.append({"role": "assistant", "content": full_text})
                final_text = full_text
                self._session_metric.add(step_metric)

                # 检查 input_queue 还有没有新事件
                pending = _drain(input_queue)
                if not pending:
                    # 4a) 没新事件 → wait_io，react 结束
                    # 持久化 final message：纯对话 turn 也得写盘，否则重启丢历史
                    await self._checkpoint.save(
                        CheckpointRecord(
                            session_key=self._session_key,
                            step_idx=self._step_idx,
                            messages=tuple(self._messages),
                            tool_results=(),
                            compressed_snapshot=None,
                        )
                    )
                    await output_queue.put(StatusChange(state="wait_io"))
                    return final_text
                # 4b) 有新事件 → aggregate，继续 react
                for ev in pending:
                    self._messages.append({"role": "user", "content": ev.text})
                continue

            # 5) tool dispatch
            self._messages.append(
                {"role": "assistant", "content": full_text or None, "tool_calls": tool_calls}
            )
            await output_queue.put(StatusChange(state="tooling"))

            has_wait_io = False
            for tc in tool_calls:
                call_id = tc.get("id", "")
                name = tc.get("function", {}).get("name", "")
                args_raw = tc.get("function", {}).get("arguments", "{}")
                args = _safe_json(args_raw)

                await output_queue.put(ToolStart(name=name, args=args))

                if name == WAIT_IO_NAME:
                    # wait_io: emit fake ok result，不真跑 tool
                    result = await self._tools.get(name).execute(call_id, args) if self._tools.get(name) else ToolResult(
                        call_id=call_id,
                        status="ok",
                        stdout="[wait_io] loop paused",
                        stderr="",
                        exit_code=0,
                    )
                    await output_queue.put(ToolEnd(name=name, result=result, latency_ms=0))
                    self._messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": format_tool_message(result)}
                    )
                    has_wait_io = True
                    continue

                tool = self._tools.get(name)
                if tool is None:
                    result = ToolResult(
                        call_id=call_id,
                        status="error",
                        stdout="",
                        stderr=f"unknown tool: {name}",
                        exit_code=1,
                    )
                    latency_ms = 0
                else:
                    t0 = time.monotonic()
                    result = await tool.execute(call_id, args)
                    latency_ms = int((time.monotonic() - t0) * 1000)

                # read_tool_result_budget 返回的是完整原始结果，不能再截断
                if name != ReadToolResultBudgetTool.name:
                    result = self._compression.compress_tool_result(result)

                await output_queue.put(ToolEnd(name=name, result=result, latency_ms=latency_ms))

                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": format_tool_message(result),
                    }
                )

            step_metric.tool_calls_count = len(tool_calls)
            self._session_metric.add(step_metric)

            await output_queue.put(MetricChunk(metrics=step_metric.snapshot()))

            await self._checkpoint.save(
                CheckpointRecord(
                    session_key=self._session_key,
                    step_idx=self._step_idx,
                    messages=tuple(self._messages),
                    tool_results=(),
                    compressed_snapshot=None,
                )
            )

            if has_wait_io:
                # wait_io: react 结束，进入 wait_io 状态
                await output_queue.put(StatusChange(state="wait_io"))
                return final_text

        return "[max_steps reached]"


def _drain(queue: asyncio.Queue[InboundEvent]) -> list[InboundEvent]:
    """非阻塞拿出 queue 里所有 element。"""
    out: list[InboundEvent] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return out


def _safe_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _merge_tool_calls(accumulated: list[dict[str, Any]], delta: tuple[dict[str, Any], ...]) -> None:
    """OpenAI 流式 tool_calls 按 index 合并。"""
    for d in delta:
        idx = d.get("index", 0)
        while len(accumulated) <= idx:
            accumulated.append(
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
        entry = accumulated[idx]
        if d.get("id"):
            entry["id"] = d["id"]
        func = entry["function"]
        d_func = d.get("function") or {}
        if d_func.get("name"):
            func["name"] = d_func["name"]
        if d_func.get("arguments"):
            func["arguments"] = (func.get("arguments") or "") + d_func["arguments"]