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
import contextvars
import json
import logging
import time
from typing import Any

import httpx

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
    ErrorEvent,
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
    ToolPending,
    ToolResult,
    ToolStart,
)
from core.skill_service import SkillService


WAIT_IO_NAME = "wait_io"

# _react 被中断时返回的哨兵 final_text；run() 据此走中断清理而不是 FinalMessage。
_INTERRUPTED = "__interrupted__"

# LLM 网络错（httpx + python builtin）。retry_stream 兜底走"system note 反馈给
# LLM 自我恢复"路径，不 abort run；不在这里的不走 retry，由外层 except 走 fatal
# ErrorEvent（见 requirements/llm-error-recovery.md）。
_RETRY_EXCEPTIONS = (
    httpx.HTTPStatusError,    # HTTP 4xx/5xx
    httpx.RequestError,       # 连接错 / socket reset / TLS
    httpx.TimeoutException,   # connect / read / pool timeout
    ConnectionError,          # python builtin 兜底
    OSError,                  # socket 资源耗尽
)

# fork_task 这类 tool 需要知道「当前在哪个 session 里执行」——Tool 协议签名没有
# session 上下文，ToolRegistry 又是全 session 共享的。engine 在 tool dispatch 处
# set 这个 contextvar（见 requirements/async-task.md「parent_session_key 从哪来」）。
_current_session_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "monox_current_session_key", default=None,
)


def current_session_key() -> str | None:
    """tool 执行期间可读：当前 dispatch 该 tool 的 session_key（engine 之外为 None）。"""
    return _current_session_key.get()


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
        skill_service: SkillService | None = None,
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
        self._skill_service = skill_service
        self._path_vars = path_vars or {}
        self._max_steps = max_steps
        # 可观测性：可选的 trace 收集器；为 None 时整条 trace 路径不执行。
        self._traces = traces
        # 当前请求使用的 provider 名（来自 user_input meta.model_provider）
        self._model_provider: str | None = None
        # 当前 react step 的 task；interrupt 会取消它。run() 的唯一 mutator，
        # is_busy 属性供 SessionManager idle sweeper 判断「step 运行中不销毁」。
        self._step_task: asyncio.Task[str] | None = None

        self._messages: list[dict[str, Any]] = []
        self._step_idx = 0
        self._session_metric = SessionMetric()
        # 当前 run / turn 的 trace_id，喂给 StatusChange / MetricChunk / FinalMessage。
        self._run_id: str | None = None
        self._current_turn_id: str | None = None

    @property
    def is_busy(self) -> bool:
        """react step 是否在跑（run() 已启动且未到下一个等待点）。"""
        return self._step_task is not None and not self._step_task.done()

    async def run(
        self,
        input_queue: asyncio.Queue[InboundEvent],
        output_queue: asyncio.Queue[StreamEvent],
    ) -> None:
        await self._restore()

        # 入站分流：interrupt 走独立队列（最高优先级，任何阶段非阻塞检查），
        # 其余事件走 sub_queue。见 spec/REQUIREMENTS/interrupt.md。
        sub_queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        interrupt_queue: asyncio.Queue[InboundEvent] = asyncio.Queue()

        async def pumper():
            while True:
                ev = await input_queue.get()
                _log.info("[pumper] session=%s ev.kind=%s event_type=%s", self._session_key, ev.kind, ev.event_type)
                if ev.kind == "interrupt":
                    await interrupt_queue.put(ev)
                else:
                    await sub_queue.put(ev)

        pump_task = asyncio.create_task(pumper())

        async def finalize_aborted(status: str) -> None:
            """中断/异常后的统一清理：回滚半截消息、关 trace、回 idle。"""
            self._messages = self._msgs_before
            self._step_idx -= 1
            self._session_metric.drop_last()
            if self._traces is not None and self._run_id is not None:
                await self._traces.end_run(None, status=status)
                self._run_id = None
                self._current_turn_id = None
            await output_queue.put(StatusChange(state="idle"))

        def take_interrupt() -> InboundEvent | None:
            """非阻塞取一条 interrupt；连发的余量一并吞掉（一波打断一次反馈）。"""
            try:
                first = interrupt_queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
            while not interrupt_queue.empty():
                interrupt_queue.get_nowait()
            return first

        async def handle_interrupt() -> None:
            """step 跑着 → cancel 它；随后统一回 idle。"""
            _log.info("[interrupt] handle_interrupt session=%s step_task=%s done=%s",
                      self._session_key,
                      self._step_task,
                      self._step_task.done() if self._step_task else True)
            if self._step_task is not None and not self._step_task.done():
                self._msgs_before = list(self._messages)
                self._step_task.cancel()
                try:
                    await asyncio.wait_for(self._step_task, timeout=5.0)
                    _log.info("[interrupt] step_task exited cleanly session=%s", self._session_key)
                except asyncio.TimeoutError:
                    _log.warning("[interrupt] step_task timed out after 5s, force-killing session=%s", self._session_key)
                except asyncio.CancelledError:
                    _log.info("[interrupt] step_task got CancelledError session=%s", self._session_key)
                except Exception as e:
                    _log.warning("[interrupt] step_task exception %s: %s session=%s", type(e).__name__, e, self._session_key)
            self._step_task = None
            await output_queue.put(StatusChange(state="idle"))
            _log.info("[interrupt] handle_interrupt done, idle emitted session=%s", self._session_key)

        async def _race_get(a: asyncio.Queue, b: asyncio.Queue, step_t: asyncio.Task | None):
            """race 两条队列 + 可选 step task，interrupt 侧赢得抢占优先。

            返回 ("msg", ev) | ("intr", ev) | ("done", None)。
            注意 step 结果由调用方 step_task.result() 取——在 helper 内 .result() 会让
            react 的异常在这里抛出，绕过调用方的 except 兜底。
            输掉的 get task 一律取消（其数据未被消费，不丢失）。
            """
            t_a = asyncio.create_task(a.get())
            t_b = asyncio.create_task(b.get())
            wait_set = {t_a, t_b}
            if step_t is not None:
                wait_set.add(step_t)
            done, _pending = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
            for t in (t_a, t_b):
                if t not in done:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
            # interrupt 赢得所有平局（最高优先级语义）
            if t_b in done:
                if step_t is not None and step_t.done():
                    # 被打断前 step 恰好完成：静默收割结果/异常，避免 unretrieved 警告
                    try:
                        step_t.result()
                    except (asyncio.CancelledError, Exception):
                        pass
                return "intr", t_b.result()
            if step_t is not None and step_t in done:
                return "done", None
            return "msg", t_a.result()

        # 当前 react step 的 task 挂在 self._step_task（见 __init__）；interrupt 会取消它。

        try:
            while True:
                # C1：主循环每轮开头先扫一轮积压 interrupt
                intr_ev = take_interrupt()
                if intr_ev is not None:
                    _log.info("[engine] session=%s C1 interrupt taken", self._session_key)
                    await handle_interrupt()
                    continue

                if self._step_task is None:
                    kind, payload = await _race_get(sub_queue, interrupt_queue, None)
                    if kind == "intr":
                        await handle_interrupt()
                        continue
                    ev = payload
                else:
                    # react 跑着：三方 race——sub_queue / interrupt 队列 / step 完成。
                    # interrupt 侧赢平局，保证 tool 长执行、LLM 卡流都可被立刻打断。
                    kind, payload = await _race_get(sub_queue, interrupt_queue, self._step_task)
                    _log.info("[engine] session=%s race result kind=%s step_task=%s", self._session_key, kind, self._step_task)
                    if kind == "intr":
                        _log.info("[engine] session=%s handling interrupt via race", self._session_key)
                        await handle_interrupt()
                        continue
                    if kind == "done":
                        try:
                            final_text = self._step_task.result()
                        except asyncio.CancelledError:
                            await finalize_aborted("cancelled")
                            self._step_task = None
                            continue
                        except Exception as exc:
                            # 真正的编程错（不是 LLM 网络错 —— 网络错在 _react 内层
                            # 已被 retry_stream + system note 路径吸收，不走到这里）：
                            # fatal 兜底。不能让异常杀死 run() 循环，否则 input_q
                            # 再无人消费 → 中断失效、UI 永远 thinking。
                            # code="internal_error"（之前是 "llm_error"，但 LLM 网络错
                            # 已不再走这里，语义修正）。
                            _log.exception("react step failed (fatal): %r", exc)
                            await output_queue.put(ErrorEvent(
                                code="internal_error",
                                msg=f"{type(exc).__name__}: {exc}",
                                retryable=False,
                            ))
                            await finalize_aborted("error")
                            self._step_task = None
                            continue

                        if final_text == _INTERRUPTED:
                            # C2/C3 协作中止路径：_react 已消费触发的那条 interrupt，
                            # 这里只做统一清理（连发余量已在 take_interrupt/_react 吞掉）。
                            await finalize_aborted("cancelled")
                            self._step_task = None
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
                        self._step_task = None
                        continue
                    ev = payload  # kind == "msg"：step 跑着收到新消息，追加进上下文由本 step 聚合

                user_msg = {"role": "user", "content": user_input_event_xml(ev)}
                self._messages.append(user_msg)
                # 提取 model_provider（来自 user_input meta），用于后续 stream() 调用
                mp = ev.meta.get("model_provider") if ev.meta else None
                if mp:
                    self._model_provider = mp
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
                self._step_task = asyncio.create_task(
                    self._react(sub_queue, interrupt_queue, output_queue)
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
        interrupt_queue: asyncio.Queue[InboundEvent],
        output_queue: asyncio.Queue[StreamEvent],
    ) -> str:
        final_text = ""

        for _ in range(self._max_steps):
            self._step_idx += 1
            step_metric = StepMetric(step_idx=self._step_idx)
            t0 = time.monotonic()

            # C2：step 开头检查中断（在 drain 新消息之前，优先级最高）。
            # 命中即消费掉这条 interrupt 并快速返回哨兵，清理统一由 run() 做。
            if not interrupt_queue.empty():
                interrupt_queue.get_nowait()
                return _INTERRUPTED

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
                self._skill_service,
                self._messages,
                self._path_vars,
            )
            tool_schemas = self._tools.schemas()

            full_text = ""
            reasoning_text = ""
            tool_calls: list[dict[str, Any]] = []
            finish_reason: str | None = None
            usage: dict | None = None
            # 每个 call_id 是否已经发过 ToolPending；OpenAI 流式 delta 第一个就 set id，
            # 后续 deltas 只是补 args —— 不要重复发。
            pending_emitted: set[str] = set()

            await output_queue.put(
                StatusChange(
                    state="thinking",
                    trace_id=self._run_id,
                    turn_id=self._current_turn_id,
                )
            )
            try:
                opts = {"model_provider": self._model_provider} if self._model_provider else None
                # LLM 网络错自动重试 3 次（指数退避 1s/2s/4s），不 abort run。
                # 详见 spec/requirements/llm-error-recovery.md。
                from core.llm_proxy.retry import retry_stream
                stream_iter = retry_stream(
                    lambda: self._llm.stream(messages, tools=tool_schemas, options=opts),
                )
                async for chunk in stream_iter:
                    # C3：流式消费循环内协作检查中断——毫秒级响应，不依赖外部 cancel。
                    # 已发出的 token 由 run() 的回滚 + idle 兜底（与 cancel 路径一致）。
                    if not interrupt_queue.empty():
                        interrupt_queue.get_nowait()
                        return _INTERRUPTED
                    if chunk.delta_text:
                        full_text += chunk.delta_text
                        await output_queue.put(TokenChunk(text=chunk.delta_text))
                    if chunk.delta_reasoning:
                        reasoning_text += chunk.delta_reasoning
                        await output_queue.put(ReasoningChunk(text=chunk.delta_reasoning))
                    if chunk.delta_tool_calls:
                        _merge_tool_calls(tool_calls, chunk.delta_tool_calls)
                        # 流式里**第一次**看到某个 call_id + name → 立刻发 ToolPending。
                        # 这样 MonoDesk 可以马上出 loading 态，不用等到 args JSON 收齐 +
                        # 解析完才出 ToolStart。
                        for d in chunk.delta_tool_calls:
                            idx = d.get("index", 0)
                            if idx >= len(tool_calls):
                                continue
                            entry = tool_calls[idx]
                            cid = entry.get("id", "")
                            if cid and cid not in pending_emitted:
                                pending_emitted.add(cid)
                                name = entry["function"].get("name", "")
                                await output_queue.put(ToolPending(
                                    call_id=cid,
                                    name=name,
                                    tool_index=idx,
                                    args_so_far=entry["function"].get("arguments", "") or "",
                                ))
                    if chunk.finish_reason:
                        finish_reason = chunk.finish_reason
                    if chunk.usage:
                        usage = chunk.usage
            except _RETRY_EXCEPTIONS as exc:
                # 重试 3 次仍失败 → 把错当 system note 反馈进 messages，让 LLM 在下一
                # 轮 react 自我决策（再试 / 改 prompt / 解释给用户）。**不** abort run，
                # **不**发 fatal ErrorEvent。已 emit 的 ToolStart 由下一轮 react 自然补救
                # （LLM 看到 system note 后会重新决策 tool 调用）；MonoDesk 端 onToolEnd
                # 的 call_id 匹配兜底处理残留 running 块（见 fix-tool-end-on-interrupt.md）。
                _log.warning(
                    "llm stream failed permanently after retries: %s — "
                    "appending system note for self-recovery (no run abort)",
                    type(exc).__name__,
                )
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
                self._messages.append({
                    "role": "user",
                    "content": (
                        f"[system note] LLM call failed 3 times: {type(exc).__name__}: {exc}. "
                        f"The provider is temporarily unreachable. Try a different approach "
                        f"(simpler request / shorter prompt / different tool sequence), or "
                        f"explain the situation to the user honestly."
                    ),
                })
                # 让 _react 以"空 assistant message"返回；run() 看到 return "" 不会走
                # finalize_aborted，主循环继续走下一轮 react step，LLM 看到 system note
                # 自我恢复。
                return ""
            except Exception as exc:
                # 真正的编程错（不是网络错）—— record trace + 让外层 except 走 fatal
                # ErrorEvent("internal_error") + finalize_aborted。
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
                    model=_llm_model(self._llm),
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
                    mp = ev.meta.get("model_provider") if ev.meta else None
                    if mp:
                        self._model_provider = mp
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

                # call_id 透传：前端把它和早些发的 ToolPending 配对成同一个块，
                # 避免「pending 块 + tool_start 又创一个」双卡片。
                await output_queue.put(ToolStart(name=name, args=args, call_id=call_id))

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
                    # 暴露「谁在调我」给 tool 层（fork_task 定位 parent 用）；见模块头注释
                    _cv_token = _current_session_key.set(self._session_key)
                    try:
                        result = await tool.execute(call_id, args)
                    except asyncio.CancelledError:
                        # interrupt 传播到这里：ToolStart 已经发了但 ToolEnd 没发，UI tool block
                        # 会卡在 running —— 必须补一条 cancelled ToolEnd 把状态收尾（详见
                        # spec/requirements/fix-tool-end-on-interrupt.md）。CancelledError
                        # 在 Python 3.8+ 继承 BaseException 不属于 Exception，必须独立 catch。
                        # _cv_token reset 交给下面 finally（这里不 reset，避免 double reset）
                        latency_ms = int((time.monotonic() - t0) * 1000)
                        cancelled_result = ToolResult(
                            call_id=call_id,
                            status="cancelled",
                            stdout="",
                            stderr="[interrupted] tool execution cancelled by user",
                            exit_code=-1,
                        )
                        await output_queue.put(ToolEnd(
                            name=name,
                            result=cancelled_result,
                            latency_ms=latency_ms,
                        ))
                        if self._traces is not None and self._current_turn_id is not None:
                            await self._traces.record_act_span(
                                self._current_turn_id,
                                tool_name=name,
                                args=args,
                                result=_tool_result_to_dict(cancelled_result),
                                latency_ms=latency_ms,
                                status="cancelled",
                            )
                        raise  # 继续传播，让外层 run() 走 finalize_aborted("cancelled")
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
                    finally:
                        _current_session_key.reset(_cv_token)
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
    """best-effort 从 LLMProxy 拿"上次 stream 用的真实 model"。

    优先取 LlmProxy._last_model：那是解析 provider 后真正发给厂家 API 的字符串
    （可能与 cfg.model 不同——cfg.model 是"人类可读名"）。

    退路：cfg.model → llm.model → "unknown"。
    """
    m = getattr(llm, "_last_model", None)
    if isinstance(m, str) and m:
        return m
    cfg = getattr(llm, "_cfg", None)
    if cfg is not None:
        m = getattr(cfg, "model", None)
        if isinstance(m, str) and m:
            return m
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