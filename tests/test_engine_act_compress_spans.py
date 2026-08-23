"""Phase 2 trace 集成测试：act (tool dispatch) + compress (L1/L2) span 真的接入。

之前 Phase 1 只验证了 reasoning span。Phase 2 在 engine 里加了：
- record_act_span（每个 tool_call 完成时记一条）
- record_compress_span（L1 在 tool result 被截断时记；L2 在 _react maybe_summarize 折叠时记）

这里跑完整 loop（mock LLM 一次 tool_call → real tool → next turn 收 final），
断言 collector 看到正确的 act / compress span。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.compression import CompressionService
from core.loop.engine import LoopEngine
from core.loop.tool_registry import ToolRegistry
from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
from core.memory import FsMemoryStore
from core.observability import JsonlTraceStore, TraceCollector
from core.protocol import InboundEvent, LlmChunk, LLMProxy, ToolResult


class _MockLLMTool(LLMProxy):
    """一轮 yield tool_call (bash)，下一轮 yield stop final。"""

    def __init__(self) -> None:
        self.call_n = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        self.call_n += 1
        if self.call_n == 1:
            yield LlmChunk(
                delta_tool_calls=(
                    {"index": 0, "id": "c1", "type": "function",
                     "function": {"name": "bash", "arguments": '{"cmd":"echo hi"}'}},
                )
            )
            yield LlmChunk(
                finish_reason="tool_calls",
                usage={"prompt_tokens": 3, "completion_tokens": 1},
            )
        else:
            yield LlmChunk(delta_text="done")
            yield LlmChunk(
                finish_reason="stop",
                usage={"prompt_tokens": 5, "completion_tokens": 2},
            )

    @property
    def model(self) -> str:
        return "mock-act"


class _FakeBashTool:
    """minimal tool: 返回 stdout 给的字符串。"""

    name = "bash"
    schema = {
        "type": "function",
        "function": {
            "name": "bash",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        },
    }

    def __init__(self, stdout: str = "hi\n", exit_code: int = 0) -> None:
        self._stdout = stdout
        self._exit_code = exit_code

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        return ToolResult(
            call_id=call_id,
            status="ok",
            stdout=self._stdout,
            stderr="",
            exit_code=self._exit_code,
        )


def _build_engine(
    tmp_path: Path,
    *,
    tool: Any,
    llm: LLMProxy,
    collector: TraceCollector | None,
    compression: CompressionService | None = None,
) -> tuple[LoopEngine, asyncio.Queue, asyncio.Queue]:
    ck_path = tmp_path / "default" / "checkpoint.jsonl"
    ck_path.parent.mkdir(parents=True, exist_ok=True)
    ck = JsonlCheckpointStore(ck_path)
    mem = FsMemoryStore(tmp_path / "mem")
    if compression is None:
        compression = CompressionService(
            budget_tool=ReadToolResultBudgetTool(),
            llm=llm,
        )
    engine = LoopEngine(
        session_key="default",
        system_prompt="sys",
        llm=llm,
        tools=ToolRegistry([tool]),
        compression=compression,
        memory=mem,
        checkpoint=ck,
                max_steps=5,
        traces=collector,
    )
    return engine, asyncio.Queue(), asyncio.Queue()


def _msg(text: str) -> InboundEvent:
    return InboundEvent(session_key="default", kind="message", text=text, source="t")


async def _drain_until_final(out_q: asyncio.Queue) -> None:
    from core.protocol import FinalMessage
    for _ in range(200):
        try:
            ev = await asyncio.wait_for(out_q.get(), timeout=0.5)
            if isinstance(ev, FinalMessage):
                return
        except asyncio.TimeoutError:
            return


async def _shutdown(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ----------------------------------------------------------------------
# act span
# ----------------------------------------------------------------------

async def test_engine_records_act_span(tmp_path: Path):
    """bash 工具被调一次 → collector 收到一条 act span (tool_name=bash, args, result)。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")
    llm = _MockLLMTool()
    engine, in_q, out_q = _build_engine(
        tmp_path, tool=_FakeBashTool(stdout="hi\n"), llm=llm, collector=collector
    )

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    await _drain_until_final(out_q)
    await asyncio.sleep(0.05)

    full = await store.get_run("default", (await store.list_runs("default"))[0].run_id)
    all_spans = [s for t in full.turns for s in t.spans]
    acts = [s for s in all_spans if s.kind.value == "act"]
    assert len(acts) == 1, all_spans
    a = acts[0]
    assert a.attributes["tool_name"] == "bash"
    assert a.attributes["args"] == {"cmd": "echo hi"}
    assert a.attributes["result"]["stdout"] == "hi\n"
    assert a.attributes["result"]["exit_code"] == 0
    # status 是 Span 的字段，不在 attributes 里
    assert a.status == "ok"
    assert a.attributes["latency_ms"] >= 0
    await _shutdown(task)


async def test_engine_records_act_span_for_unknown_tool(tmp_path: Path):
    """agent 调不存在的 tool → 第一条 act span status=error（之后 llm 再 final 收尾）。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")

    class _BadThenFinalLLM(LLMProxy):
        async def stream(self, messages, tools=None, options=None) -> AsyncIterator[LlmChunk]:
            # 第一次：tool_call 调一个不存在的 tool
            if not getattr(self, "_called", False):
                self._called = True
                yield LlmChunk(
                    delta_tool_calls=(
                        {"index": 0, "id": "c1", "type": "function",
                         "function": {"name": "no_such_tool", "arguments": "{}"}},
                    )
                )
                yield LlmChunk(
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 1, "completion_tokens": 1},
                )
            else:
                yield LlmChunk(delta_text="done")
                yield LlmChunk(
                    finish_reason="stop",
                    usage={"prompt_tokens": 1, "completion_tokens": 1},
                )

        @property
        def model(self) -> str:
            return "bad"

    llm = _BadThenFinalLLM()
    engine, in_q, out_q = _build_engine(
        tmp_path, tool=_FakeBashTool(), llm=llm, collector=collector
    )

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    await _drain_until_final(out_q)
    await asyncio.sleep(0.05)

    full = await store.get_run("default", (await store.list_runs("default"))[0].run_id)
    acts = [s for t in full.turns for s in t.spans if s.kind.value == "act"]
    unknown_acts = [a for a in acts if a.attributes["tool_name"] == "no_such_tool"]
    assert len(unknown_acts) == 1
    assert unknown_acts[0].status == "error"
    assert "unknown tool" in unknown_acts[0].attributes["result"]["stderr"]
    await _shutdown(task)


async def test_engine_records_act_span_for_wait_io(tmp_path: Path):
    """wait_io 也记一条 act span（status=ok，loop paused 是合法状态）。"""
    from core.loop import WaitIoTool

    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")

    class _WaitLLM(LLMProxy):
        async def stream(self, messages, tools=None, options=None) -> AsyncIterator[LlmChunk]:
            yield LlmChunk(
                delta_tool_calls=(
                    {"index": 0, "id": "c1", "type": "function",
                     "function": {"name": "wait_io", "arguments": "{}"}},
                )
            )
            yield LlmChunk(
                finish_reason="tool_calls",
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )

        @property
        def model(self) -> str:
            return "wait"

    ck_path = tmp_path / "default" / "checkpoint.jsonl"
    ck_path.parent.mkdir(parents=True, exist_ok=True)
    ck = JsonlCheckpointStore(ck_path)
    mem = FsMemoryStore(tmp_path / "mem")
    comp = CompressionService(
        budget_tool=ReadToolResultBudgetTool(),
        llm=_WaitLLM(),
    )
    engine = LoopEngine(
        session_key="default",
        system_prompt="sys",
        llm=_WaitLLM(),
        tools=ToolRegistry([WaitIoTool()]),
        compression=comp,
        memory=mem,
        checkpoint=ck,
                max_steps=5,
        traces=collector,
    )
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    await _drain_until_final(out_q)
    await asyncio.sleep(0.05)

    full = await store.get_run("default", (await store.list_runs("default"))[0].run_id)
    acts = [s for t in full.turns for s in t.spans if s.kind.value == "act"]
    assert len(acts) == 1
    assert acts[0].attributes["tool_name"] == "wait_io"
    assert acts[0].status == "ok"
    await _shutdown(task)


# ----------------------------------------------------------------------
# L1 compress span（tool result 超 4000 字符触发 L1 截断）
# ----------------------------------------------------------------------

async def test_engine_records_l1_compress_span_on_long_tool_output(tmp_path: Path):
    """bash 输出超 4000 字符 → L1 截断 → 同时记 act span + compress:L1 span。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")
    llm = _MockLLMTool()
    long_stdout = "x" * 8000  # L1 阈值是 4000
    engine, in_q, out_q = _build_engine(
        tmp_path, tool=_FakeBashTool(stdout=long_stdout), llm=llm, collector=collector
    )

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    await _drain_until_final(out_q)
    await asyncio.sleep(0.05)

    full = await store.get_run("default", (await store.list_runs("default"))[0].run_id)
    all_spans = [s for t in full.turns for s in t.spans]
    by_kind = {s.kind.value: s for s in all_spans}
    assert "act" in by_kind
    assert "compress" in by_kind
    comp = by_kind["compress"]
    assert comp.attributes["level"] == "L1"
    assert comp.attributes["folded_count"] == 1
    assert isinstance(comp.attributes["budget_ids"], list)
    assert len(comp.attributes["budget_ids"]) == 1
    assert comp.attributes["budget_ids"][0]  # 非空 hex id
    # act span 的 result 应该已经 truncated=True
    assert by_kind["act"].attributes["result"]["truncated"] is True
    await _shutdown(task)


async def test_engine_no_compress_span_when_tool_output_short(tmp_path: Path):
    """bash 输出 < 4000 字符 → L1 不触发 → 不记 compress span。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")
    llm = _MockLLMTool()
    engine, in_q, out_q = _build_engine(
        tmp_path, tool=_FakeBashTool(stdout="short"), llm=llm, collector=collector
    )

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    await _drain_until_final(out_q)
    await asyncio.sleep(0.05)

    full = await store.get_run("default", (await store.list_runs("default"))[0].run_id)
    all_spans = [s for t in full.turns for s in t.spans]
    compresses = [s for s in all_spans if s.kind.value == "compress"]
    assert compresses == []
    await _shutdown(task)


# ----------------------------------------------------------------------
# L2 compress span（messages 超 24k 字符触发 L2 summary）
# ----------------------------------------------------------------------

async def test_engine_records_l2_compress_span_on_long_history(tmp_path: Path):
    """messages > 24k 字符 → L2 折叠 → engine 记 compress:L2 span。"""
    from core.loop.compression import L2_CHAR_THRESHOLD

    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")

    # scripted LLM：第一次 call 是 L2 summary（前置），第二次是真正的 chat。
    class _TwoStageLLM(LLMProxy):
        def __init__(self) -> None:
            self.n = 0

        async def stream(self, messages, tools=None, options=None) -> AsyncIterator[LlmChunk]:
            self.n += 1
            if self.n == 1:
                # 第一次：L2 summary call，必须返回 summary 文本
                yield LlmChunk(delta_text="folded history summary")
                yield LlmChunk(
                    finish_reason="stop",
                    usage={"prompt_tokens": 2, "completion_tokens": 4},
                )
            else:
                # 第二次：真正的 chat call，触发 final
                yield LlmChunk(delta_text="ok")
                yield LlmChunk(
                    finish_reason="stop",
                    usage={"prompt_tokens": 1, "completion_tokens": 1},
                )

        @property
        def model(self) -> str:
            return "l2"

    llm = _TwoStageLLM()
    engine, in_q, out_q = _build_engine(
        tmp_path, tool=_FakeBashTool(stdout="ok"), llm=llm, collector=collector
    )

    # 预填历史：4 个 user turn + 4 个 assistant，每个 user content 6000 字符
    # 总字符数 > L2_CHAR_THRESHOLD (24000) 且 user 轮数 > L2_KEEP_TURNS (2)
    big = "x" * 6000
    engine._messages = [
        {"role": "user", "content": f"turn1 {big}"},
        {"role": "assistant", "content": "ok1"},
        {"role": "user", "content": f"turn2 {big}"},
        {"role": "assistant", "content": "ok2"},
        {"role": "user", "content": f"turn3 {big}"},
        {"role": "assistant", "content": "ok3"},
        {"role": "user", "content": f"turn4 {big}"},
    ]
    # step_idx 推一段：上面有 4 个 user turn，react 第一轮会算 step_idx
    engine._step_idx = 0

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("final"))  # 触发新一轮
    await _drain_until_final(out_q)
    await asyncio.sleep(0.05)

    full = await store.get_run("default", (await store.list_runs("default"))[0].run_id)
    all_spans = [s for t in full.turns for s in t.spans]
    l2 = [s for s in all_spans if s.kind.value == "compress" and s.attributes.get("level") == "L2"]
    assert len(l2) == 1, [s.kind.value + "/" + str(s.attributes.get("level")) for s in all_spans]
    assert l2[0].attributes["folded_count"] > 0
    assert "folded history summary" in l2[0].attributes["summary"]
    # 验证 LLM 确实被调用了 2 次（一次 summary，一次 chat）
    assert llm.n == 2
    await _shutdown(task)


# ----------------------------------------------------------------------
# #3 reasoning_content 拼接到 span
# ----------------------------------------------------------------------

async def test_reasoning_span_records_reasoning_content(tmp_path: Path):
    """LLM stream yield reasoning_content_delta → record_llm_span 拼完整内容。"""

    class _ReasoningLLM(LLMProxy):
        async def stream(self, messages, tools=None, options=None) -> AsyncIterator[LlmChunk]:
            yield LlmChunk(delta_reasoning="让我想想...")
            yield LlmChunk(delta_reasoning=" 该用 grep")
            yield LlmChunk(delta_text="ok")
            yield LlmChunk(
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 8},
            )

        @property
        def model(self) -> str:
            return "r1"

    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")
    engine, in_q, out_q = _build_engine(
        tmp_path, tool=_FakeBashTool(), llm=_ReasoningLLM(), collector=collector
    )

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    await _drain_until_final(out_q)
    await asyncio.sleep(0.05)

    full = await store.get_run("default", (await store.list_runs("default"))[0].run_id)
    reasoning = [s for t in full.turns for s in t.spans if s.kind.value == "reasoning"]
    assert len(reasoning) == 1
    r = reasoning[0]
    assert r.attributes["reasoning_content"] == "让我想想... 该用 grep"
    assert r.attributes["usage"]["cached_tokens"] == 8  # 透传
    await _shutdown(task)


# ----------------------------------------------------------------------
# #4 tool.execute 异常路径：自定义 tool 直接 raise 也保证记 act span
# ----------------------------------------------------------------------

async def test_engine_records_act_span_when_tool_raises(tmp_path: Path):
    """tool.execute 直接 raise（非返回 status=error 的 ToolResult）也保证记一条 act span。"""

    class _BoomTool:
        name = "boom"
        schema = {"type": "function", "function": {"name": "boom", "parameters": {}}}

        async def execute(self, call_id: str, arguments: dict):
            raise RuntimeError("kaboom")

    class _BoomLLM(LLMProxy):
        def __init__(self) -> None:
            self._called = False

        async def stream(self, messages, tools=None, options=None) -> AsyncIterator[LlmChunk]:
            if not self._called:
                self._called = True
                yield LlmChunk(
                    delta_tool_calls=(
                        {"index": 0, "id": "c1", "type": "function",
                         "function": {"name": "boom", "arguments": "{}"}},
                    )
                )
                yield LlmChunk(
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 1, "completion_tokens": 1},
                )
            else:
                yield LlmChunk(delta_text="done")
                yield LlmChunk(
                    finish_reason="stop",
                    usage={"prompt_tokens": 1, "completion_tokens": 1},
                )

        @property
        def model(self) -> str:
            return "boom"

    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")
    llm = _BoomLLM()
    engine, in_q, out_q = _build_engine(
        tmp_path, tool=_BoomTool(), llm=llm, collector=collector
    )

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    await _drain_until_final(out_q)
    await asyncio.sleep(0.05)

    full = await store.get_run("default", (await store.list_runs("default"))[0].run_id)
    acts = [s for t in full.turns for s in t.spans if s.kind.value == "act"]
    assert len(acts) == 1
    assert acts[0].attributes["tool_name"] == "boom"
    assert acts[0].status == "error"
    assert "RuntimeError" in acts[0].attributes["result"]["stderr"]
    assert "kaboom" in acts[0].attributes["result"]["stderr"]
    await _shutdown(task)


# ----------------------------------------------------------------------
# #2 Span.parent_id = turn_id
# ----------------------------------------------------------------------

async def test_spans_have_parent_id_equal_to_turn_id(tmp_path: Path):
    """所有 span 的 parent_id 应该等于其所在 turn 的 turn_id。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    collector = TraceCollector(store, "default")
    llm = _MockLLMTool()
    engine, in_q, out_q = _build_engine(
        tmp_path, tool=_FakeBashTool(stdout="x" * 8000), llm=llm, collector=collector
    )

    task = asyncio.create_task(engine.run(in_q, out_q))
    in_q.put_nowait(_msg("hi"))
    await _drain_until_final(out_q)
    await asyncio.sleep(0.05)

    full = await store.get_run("default", (await store.list_runs("default"))[0].run_id)
    for turn in full.turns:
        for span in turn.spans:
            assert span.parent_id == turn.turn_id, (
                f"span {span.span_id} parent_id={span.parent_id} != turn {turn.turn_id}"
            )
    await _shutdown(task)