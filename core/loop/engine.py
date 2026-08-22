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

        # 持续在后台 pump input_queue → 内部 sub_queue（保证 main loop 的 await 不会阻塞 react）
        sub_queue: asyncio.Queue[InboundEvent] = asyncio.Queue()

        async def pumper():
            while True:
                ev = await input_queue.get()
                await sub_queue.put(ev)

        pump_task = asyncio.create_task(pumper())

        # 当前 react step 的 task；interrupt 会取消它。
        step_task: asyncio.Task[str] | None = None

        async def next_input_or_done() -> tuple[InboundEvent | None, str | None]:
            """等下一个 input 事件，或当前 step 完成。

            返回 (input_event, final_text)；二者只有一个非 None。
            """
            nonlocal step_task
            # 起一个 task 等 sub_queue
            get_task = asyncio.create_task(sub_queue.get())
            try:
                if step_task is None:
                    ev = await get_task
                    return ev, None
                # 同时等 input 和 step 哪个先到
                done, pending = await asyncio.wait(
                    {get_task, step_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if step_task in done:
                    # react 完成；cancel 等 sub_queue 的 task
                    get_task.cancel()
                    try: await get_task
                    except: pass
                    final = step_task.result()
                    return None, final
                else:
                    # input 先到；cancel step（仅当 step 已跑完；否则让它继续）
                    ev = get_task.result()
                    return ev, None
            except asyncio.CancelledError:
                if not get_task.done():
                    get_task.cancel()
                raise

        try:
            while True:
                if step_task is None:
                    ev = await sub_queue.get()
                else:
                    # react 跑着，等 interrupt 帧或 react 完成
                    ev, final_text = await self._select(sub_queue, step_task)
                    if ev is None:
                        # react 完成
                        try:
                            final_text = step_task.result()
                        except asyncio.CancelledError:
                            self._messages = self._msgs_before
                            self._step_idx -= 1
                            self._session_metric.drop_last()
                            await output_queue.put(StatusChange(state="idle"))
                            step_task = None
                            continue
                        await output_queue.put(
                            FinalMessage(text=final_text, metrics=self._session_metric.snapshot())
                        )
                        step_task = None
                        continue

                if ev.kind == "interrupt":
                    # 真打断：取消正在跑的 step task，不污染 messages。
                    if step_task is not None and not step_task.done():
                        self._msgs_before = list(self._messages)
                        step_task.cancel()
                        try:
                            await asyncio.wait_for(step_task, timeout=5.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                            pass
                    step_task = None
                    await output_queue.put(StatusChange(state="idle"))
                    # drain 余下 interrupt；非 interrupt 事件放回 sub_queue 队首
                    first_non_interrupt: InboundEvent | None = None
                    while not sub_queue.empty():
                        try:
                            nxt = sub_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if nxt.kind == "interrupt":
                            continue
                        first_non_interrupt = nxt
                        break
                    if first_non_interrupt is not None:
                        await sub_queue.put(first_non_interrupt)
                    continue

                self._messages.append({"role": "user", "content": ev.text})

                # react 前快照 messages；cancel 后回滚到该状态
                self._msgs_before = list(self._messages)
                step_task = asyncio.create_task(
                    self._react(sub_queue, output_queue)
                )
        finally:
            pump_task.cancel()
            try: await pump_task
            except: pass

    @staticmethod
    async def _select(
        queue: asyncio.Queue[InboundEvent],
        step_task: asyncio.Task[str],
    ) -> tuple[InboundEvent | None, str | None]:
        """等 queue.get() 或 step_task 完成；先到先返回。"""
        get_task = asyncio.create_task(queue.get())
        try:
            done, pending = await asyncio.wait(
                {get_task, step_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if step_task in done:
                get_task.cancel()
                try: await get_task
                except: pass
                return None, step_task.result()
            else:
                return get_task.result(), None
        except asyncio.CancelledError:
            if not get_task.done():
                get_task.cancel()
            raise

    async def _restore(self) -> None:
        ck = await self._checkpoint.load_latest(self._session_key)
        if ck is None:
            return
        self._messages = list(ck.messages)
        self._step_idx = ck.step_idx + 1

    @staticmethod
    async def _await_step(t: asyncio.Task) -> str:
        """包装 task await，让 step_task.cancelled() 变成正常返回而不是抛 CancelledError。"""
        return await t

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