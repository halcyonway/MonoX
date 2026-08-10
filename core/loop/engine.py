"""ReAct 主循环 + 状态机。

状态：idle → thinking → tooling → ... → idle → done
v0 wait_io 通过 inbound.interrupt 触发（cancel 当前 react）。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from core.loop.context import (
    assemble_messages,
    compress_tool_result,
    format_tool_message,
)
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
    StatusChange,
    StreamEvent,
    TokenChunk,
    ToolEnd,
    ToolResult,
    ToolStart,
)


class LoopEngine:
    def __init__(
        self,
        *,
        session_key: str,
        system_prompt: str,
        llm: LLMProxy,
        tools: ToolRegistry,
        budget_tool: ReadToolResultBudgetTool,
        memory: MemoryStore,
        checkpoint: CheckpointStore,
        skill_summary: str,
        max_steps: int = 30,
    ) -> None:
        self._session_key = session_key
        self._system = system_prompt
        self._llm = llm
        self._tools = tools
        self._budget_tool = budget_tool
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
            if ev.kind == "interrupt":
                continue

            self._messages.append({"role": "user", "content": ev.text})
            await output_queue.put(StatusChange(state="thinking"))

            final_text = await self._react(output_queue)

            await output_queue.put(
                FinalMessage(text=final_text, metrics=self._session_metric.snapshot())
            )

    async def _restore(self) -> None:
        ck = await self._checkpoint.load_latest(self._session_key)
        if ck is None:
            return
        self._messages = list(ck.messages)
        self._step_idx = ck.step_idx + 1

    async def _react(self, output_queue: asyncio.Queue[StreamEvent]) -> str:
        final_text = ""

        for _ in range(self._max_steps):
            self._step_idx += 1
            step_metric = StepMetric(step_idx=self._step_idx)
            t0 = time.monotonic()

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
                if chunk.delta_tool_calls:
                    _merge_tool_calls(tool_calls, chunk.delta_tool_calls)
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
                if chunk.usage:
                    usage = chunk.usage

            step_metric.latency_ms = int((time.monotonic() - t0) * 1000)
            step_metric.tokens = usage

            if not tool_calls or finish_reason == "stop":
                self._messages.append({"role": "assistant", "content": full_text})
                final_text = full_text
                self._session_metric.add(step_metric)
                await output_queue.put(StatusChange(state="idle"))
                return final_text

            self._messages.append(
                {"role": "assistant", "content": full_text or None, "tool_calls": tool_calls}
            )
            await output_queue.put(StatusChange(state="tooling"))

            for tc in tool_calls:
                call_id = tc.get("id", "")
                name = tc.get("function", {}).get("name", "")
                args_raw = tc.get("function", {}).get("arguments", "{}")
                args = _safe_json(args_raw)

                await output_queue.put(ToolStart(name=name, args=args))

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

                result = compress_tool_result(result, self._budget_tool)

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

        return "[max_steps reached]"


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