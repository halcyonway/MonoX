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
import logging
import time
from typing import Any

_log = logging.getLogger("monox.loop.engine")

from core.loop.compression import CompressionService
from core.loop.context import assemble_messages
from core.loop.event_format import tool_result_event_xml, user_input_event_xml
from core.loop.metric import SessionMetric, StepMetric
from core.loop.tool_registry import ToolRegistry
from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
from core.observability.collector import TraceCollector
from core.protocol import (
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
        path_vars: dict[str, str] | None = None,
        max_steps: int = 30,
        traces: TraceCollector | None = None,
    ) -> None:
        self._session_key = session_key
        self._system = system_prompt
        self._llm = llm
        self._tools = tools
        self._compression = compression
        self._memory = memory
        self._checkpoint = checkpoint
        self._skill_summary = skill_summary
        self._path_vars = path_vars or {}
        self._max_steps = max_steps
        # 可观测性：可选的 trace 收集器；为 None 时整条 trace 路径不执行。
        self._traces = traces

        self._messages: list[dict[str, Any]] = []
        self._step_idx = 0
        self._session_metric = SessionMetric()
        # 当前 run / turn 的 trace_id，喂给 StatusChange / MetricChunk / FinalMessage。
        self._run_id: str | None = None
        self._current_turn_id: str | None = None

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
                            # 可观测性：cancelled 路径关 run
                            if self._traces is not None and self._run_id is not None:
                                await self._traces.end_run(None, status="cancelled")
                                self._run_id = None
                                self._current_turn_id = None
                            await output_queue.put(StatusChange(state="idle"))
                            step_task = None
                            continue
                        await output_queue.put(
                            FinalMessage(
                                text=final_text,
                                metrics=self._session_metric.snapshot(),
                                trace_id=self._run_id,
                            )
                        )
                        # 可观测性：正常结束 run
                        if self._traces is not None and self._run_id is not None:
                            await self._traces.end_run(final_text, status="ok")
                            self._run_id = None
                            self._current_turn_id = None
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

                user_msg = {"role": "user", "content": user_input_event_xml(ev)}
                self._messages.append(user_msg)
                # 持久化：用户消息到达是稳定边界，立刻 append
                await self._checkpoint.append(
                    self._session_key,
                    {"kind": "msg", **user_msg},
                )

                # react 前快照 messages；cancel 后回滚到该状态
                self._msgs_before = list(self._messages)
                # 可观测性：起一次新 run；记录后 self._run_id 可用于 stamp 后续事件
                if self._traces is not None and self._run_id is None:
                    self._run_id = await self._traces.begin_run(ev.text)
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
        """从 append-only checkpoint log 重建状态。

        load_messages 从最近的 compact 节点开始 replay msg 事件；空文件返回 []。
        step_idx 推算为恢复后 messages 里 assistant 消息数（每个 assistant 对应一个 react step）。
        """
        msgs = await self._checkpoint.load_messages(self._session_key)
        if not msgs:
            return
        self._messages = list(msgs)
        # 每个 assistant message 对应一次 react step 完成
        self._step_idx = sum(1 for m in msgs if m.get("role") == "assistant")

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
                msg = {"role": "user", "content": user_input_event_xml(ev)}
                self._messages.append(msg)
                await self._checkpoint.append(
                    self._session_key,
                    {"kind": "msg", **msg},
                )

            # 可观测性：每个 turn 起一个 turn span（必须在 L2 折叠之前，否则
            # compress span 找不到 parent turn）。
            if self._traces is not None and self._run_id is not None:
                self._current_turn_id = await self._traces.begin_turn(self._step_idx)

            # 2) L2 压缩（若需要）→ assemble + LLM stream
            if self._compression.should_compress(self._messages):
                await output_queue.put(StatusChange(state="compressing"))

            self._messages, l2_summary, l2_folded = (
                await self._compression.summarize_for_trace(self._messages)
            )
            # 持久化：L2 折叠是一个"压缩节点"，checkpoint append 一条 compact 事件。
            # compressed_messages 是折叠后的新基底——重启时从这里开始 replay 后续 msg 事件。
            if l2_summary is not None and l2_folded > 0:
                await self._checkpoint.append(
                    self._session_key,
                    {
                        "kind": "compact",
                        "step": self._step_idx,
                        "summary": l2_summary,
                        "folded_count": l2_folded,
                        "compressed_messages": list(self._messages),
                    },
                )
            # 可观测性：L2 真的折叠时才记一条 compress span（summary 非空 + folded > 0）。
            if (
                l2_summary is not None
                and l2_folded > 0
                and self._traces is not None
                and self._current_turn_id is not None
            ):
                await self._traces.record_compress_span(
                    self._current_turn_id,
                    level="L2",
                    summary=l2_summary,
                    folded_count=l2_folded,
                    budget_ids=None,
                )

            # 注入 system prompt 的 `## Memory` section 内容。
            memory_index = await self._memory.read_index(self._session_key)
            messages = assemble_messages(
                self._system,
                memory_index,
                self._skill_summary,
                self._messages,
                self._path_vars,
            )
            tool_schemas = self._tools.schemas()

            full_text = ""
            reasoning_text = ""
            tool_calls: list[dict[str, Any]] = []
            finish_reason: str | None = None
            usage: dict | None = None

            await output_queue.put(
                StatusChange(
                    state="thinking",
                    trace_id=self._run_id,
                    turn_id=self._current_turn_id,
                )
            )
            try:
                async for chunk in self._llm.stream(messages, tools=tool_schemas):
                    if chunk.delta_text:
                        full_text += chunk.delta_text
                        await output_queue.put(TokenChunk(text=chunk.delta_text))
                    if chunk.delta_reasoning:
                        reasoning_text += chunk.delta_reasoning
                        await output_queue.put(ReasoningChunk(text=chunk.delta_reasoning))
                    if chunk.delta_tool_calls:
                        _merge_tool_calls(tool_calls, chunk.delta_tool_calls)
                    if chunk.finish_reason:
                        finish_reason = chunk.finish_reason
                    if chunk.usage:
                        usage = chunk.usage
            except Exception as exc:
                # 可观测性：LLM 失败也记一条 reasoning span，状态=error。
                if self._traces is not None and self._current_turn_id is not None:
                    await self._traces.record_llm_span(
                        self._current_turn_id,
                        model=_llm_model(self._llm),
                        messages=messages,
                        response_text=full_text,
                        reasoning_content=reasoning_text or None,
                        usage=usage,
                        finish_reason=finish_reason,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        status="error",
                    )
                raise

            step_metric.latency_ms = int((time.monotonic() - t0) * 1000)
            step_metric.tokens = usage

            # 每个 step 完成后立即发 MetricChunk（不依赖后面是 final 还是 tool 路径）。
            # 之前只 tool dispatch 之后才发，导致纯对话 turn（agent finish_reason=stop，
            # 不调 tool）→ 没 metric chunk → MonoDesk 看不到 tokens / cache。
            await output_queue.put(
                MetricChunk(
                    metrics=step_metric.snapshot(),
                    trace_id=self._run_id,
                    turn_id=self._current_turn_id,
                )
            )

            # 调试日志：trace / MetricChunk 携带的 usage 状态。
            # usage=None 通常意味着上游没传 stream_options.include_usage，
            # 或者代理被换成了不支持该字段的实现 —— 从这条日志直接看出来。
            cached = usage.get("cached_tokens") if isinstance(usage, dict) else None
            _log.info(
                "llm turn done step=%d latency_ms=%d prompt=%s completion=%s cached=%s",
                step_metric.step_idx,
                step_metric.latency_ms,
                usage.get("prompt_tokens") if isinstance(usage, dict) else None,
                usage.get("completion_tokens") if isinstance(usage, dict) else None,
                cached,
            )

            # 可观测性：成功路径记录完整 reasoning span（messages in / out / usage）。
            if self._traces is not None and self._current_turn_id is not None:
                await self._traces.record_llm_span(
                    self._current_turn_id,
                    model=_llm_model(self._llm),
                    messages=messages,
                    response_text=full_text,
                    reasoning_content=reasoning_text or None,
                    usage=usage,
                    finish_reason=finish_reason,
                    latency_ms=step_metric.latency_ms,
                    status="ok",
                )

            # 3) final message 分支（agent 没调 tool 或 finish_reason=stop）
            if not tool_calls or finish_reason == "stop":
                assistant_msg = {"role": "assistant", "content": full_text}
                self._messages.append(assistant_msg)
                await self._checkpoint.append(
                    self._session_key,
                    {"kind": "msg", **assistant_msg},
                )
                final_text = full_text
                self._session_metric.add(step_metric)

                # 检查 input_queue 还有没有新事件
                pending = _drain(input_queue)
                if not pending:
                    # 4a) 没新事件 → wait_io，react 结束
                    await output_queue.put(StatusChange(state="wait_io"))
                    return final_text
                # 4b) 有新事件 → aggregate，继续 react
                for ev in pending:
                    msg = {"role": "user", "content": user_input_event_xml(ev)}
                    self._messages.append(msg)
                    await self._checkpoint.append(
                        self._session_key,
                        {"kind": "msg", **msg},
                    )
                continue

            # 5) tool dispatch
            assistant_msg = {
                "role": "assistant",
                "content": full_text or None,
                "tool_calls": tool_calls,
            }
            self._messages.append(assistant_msg)
            await self._checkpoint.append(
                self._session_key,
                {"kind": "msg", **assistant_msg},
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
                    # 可观测性：wait_io 是循环暂停信号，记一条 act span 让 UI 能渲染。
                    if self._traces is not None and self._current_turn_id is not None:
                        await self._traces.record_act_span(
                            self._current_turn_id,
                            tool_name=name,
                            args=args,
                            result=_tool_result_to_dict(result),
                            latency_ms=0,
                            status="ok",
                        )
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": tool_result_event_xml(call_id, result, tool=name),
                    }
                    self._messages.append(tool_msg)
                    await self._checkpoint.append(
                        self._session_key,
                        {"kind": "msg", **tool_msg},
                    )
                    has_wait_io = True
                    continue

                tool = self._tools.get(name)
                tool_status = "ok"
                if tool is None:
                    result = ToolResult(
                        call_id=call_id,
                        status="error",
                        stdout="",
                        stderr=f"unknown tool: {name}",
                        exit_code=1,
                    )
                    latency_ms = 0
                    tool_status = "error"
                else:
                    t0 = time.monotonic()
                    try:
                        result = await tool.execute(call_id, args)
                    except Exception as exc:
                        # 自定义 tool 实现可能直接 raise（非返回 status=error 的 ToolResult）。
                        # 这里兜住，保证 trace 里能看到这条失败调用。
                        result = ToolResult(
                            call_id=call_id,
                            status="error",
                            stdout="",
                            stderr=f"{type(exc).__name__}: {exc}",
                            exit_code=-1,
                        )
                        tool_status = "error"
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    if result.status == "error":
                        tool_status = "error"

                # read_tool_result_budget 返回的是完整原始结果，不能再截断
                was_truncated = result.truncated
                if name != ReadToolResultBudgetTool.name:
                    result = self._compression.compress_tool_result(result)

                await output_queue.put(ToolEnd(name=name, result=result, latency_ms=latency_ms))

                # 可观测性：act span 记 tool 调用全貌（args / result / latency / status）。
                if self._traces is not None and self._current_turn_id is not None:
                    await self._traces.record_act_span(
                        self._current_turn_id,
                        tool_name=name,
                        args=args,
                        result=_tool_result_to_dict(result),
                        latency_ms=latency_ms,
                        status=tool_status,
                    )
                    # L1 实际发生了折叠（result.truncated 翻 True）才记 compress span。
                    if not was_truncated and result.truncated:
                        await self._traces.record_compress_span(
                            self._current_turn_id,
                            level="L1",
                            summary=result.stdout[:200],  # 摘要：截断后前 200 字符
                            folded_count=1,
                            budget_ids=[result.budget_id] if result.budget_id else None,
                        )

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": tool_result_event_xml(call_id, result, tool=name),
                }
                self._messages.append(tool_msg)
                await self._checkpoint.append(
                    self._session_key,
                    {"kind": "msg", **tool_msg},
                )

            step_metric.tool_calls_count = len(tool_calls)
            self._session_metric.add(step_metric)

            # 注意：MetricChunk 已经在 step 完成时统一发过一次（line ~360），不再重复。

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


def _llm_model(llm: Any) -> str:
    """best-effort 从 LLMProxy 拿 model 名（不在 Protocol 里，做 duck-typing）。"""
    # 优先：cfg.model（OpenAIStreamProxy 走这里）
    cfg = getattr(llm, "_cfg", None)
    if cfg is not None:
        m = getattr(cfg, "model", None)
        if isinstance(m, str) and m:
            return m
    # 退路：直接的 model 属性（测试 / 自定义 proxy）
    m = getattr(llm, "model", None)
    if isinstance(m, str) and m:
        return m
    return "unknown"


def _tool_result_to_dict(result: ToolResult) -> dict[str, Any]:
    """ToolResult → dict（trace span attributes 用）。"""
    return {
        "call_id": result.call_id,
        "status": result.status,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "truncated": result.truncated,
        "budget_id": result.budget_id,
    }


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