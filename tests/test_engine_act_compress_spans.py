"""Trace 集成测试：tool/act + compress (L1/L2) span 真的接入。

v2 模型：
- 每个 tool_call 触发 `record_tool_span`（kind=tool，OTel tool.* 字段）
- 同 turn 的所有 tool_call 共享一个 ACT 容器 span（kind=act，记录 tool_calls_count）
- L1 截断 → record_compress_span(level="L1")；L2 折叠 → record_compress_span(level="L2")
- reasoning span 走 OTel 字段（gen_ai.response.reasoning / gen_ai.usage.cached_tokens 等）

OTel 规定 tool.call.arguments / tool.result 是 string；MonoX 存 JSON 字符串。
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
from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
from core.memory import FsMemoryStore
from core.observability import JsonlTraceStore, TraceCollector
from core.observability.otel_attrs import (
    ATTR_GENAI_CLIENT_OPERATION_DURATION,
    ATTR_GENAI_RESPONSE_REASONING,
    ATTR_GENAI_USAGE_CACHED_TOKENS,
    ATTR_LOOP_COMPRESS_BUDGETS,
    ATTR_LOOP_COMPRESS_FOLDED,
    ATTR_LOOP_COMPRESS_LEVEL,
    ATTR_LOOP_COMPRESS_SUMMARY,
    ATTR_TOOL_CALL_ARGUMENTS,
    ATTR_TOOL_NAME,
    ATTR_TOOL_RESULT,
)
from core.observability.types import SpanKind
from core.protocol import InboundEvent, LlmChunk, LLMProxy, ToolResult

# memory 功能后 assemble_messages 的 memory_section 需要 path_vars（run.py 装配时提供）
_PATH_VARS = {
    "MONOX_HOME": "/tmp",
    "MONOX_WORKSPACE_DIR": "/tmp/ws",
    "MONOX_MEMORY_DIR": "/tmp/mem",
    "MONOX_SKILLS_DIR": "/tmp/skills",
    "MONOX_TMP_DIR": "/tmp/tmp",
}



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
        path_vars=_PATH_VARS,
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
# tool / act span
# ----------------------------------------------------------------------

async def test_engine_records_tool_span(tmp_path: Path):
    """bash 工具被调一次 → collector 收到 ACT 容器 + TOOL span（OTel tool.* 字段）。"""
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
    tools = [s for s in all_spans if s.kind == SpanKind.TOOL]
    acts = [s for s in all_spans if s.kind == SpanKind.ACT]
    assert len(tools) == 1, all_spans
    assert len(acts) == 1
    t = tools[0]
    assert t.attributes[ATTR_TOOL_NAME] == "bash"
    # OTel 规定 args / result 是 string；MonoX 存 JSON 字符串
    assert json.loads(t.attributes[ATTR_TOOL_CALL_ARGUMENTS]) == {"cmd": "echo hi"}
    parsed_result = json.loads(t.attributes[ATTR_TOOL_RESULT])
    assert parsed_result["stdout"] == "hi\n"
    assert parsed_result["exit_code"] == 0
    assert t.status == "ok"
    assert t.attributes[ATTR_GENAI_CLIENT_OPERATION_DURATION] >= 0
    # tool 挂在 act 容器下
    assert t.parent_id == acts[0].span_id
    await _shutdown(task)


async def test_engine_records_tool_span_for_unknown_tool(tmp_path: Path):
    """agent 调不存在的 tool → TOOL span status=error（之后 llm 再 final 收尾）。"""
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
    all_spans = [s for t in full.turns for s in t.spans]
    unknown_tools = [t for t in all_spans if t.kind == SpanKind.TOOL and t.attributes.get(ATTR_TOOL_NAME) == "no_such_tool"]
    assert len(unknown_tools) == 1
    assert unknown_tools[0].status == "error"
    parsed = json.loads(unknown_tools[0].attributes[ATTR_TOOL_RESULT])
    assert "unknown tool" in parsed["stderr"]
    await _shutdown(task)


async def test_engine_records_tool_span_for_wait_io(tmp_path: Path):
    """wait_io 也记一条 TOOL span（status=ok，loop paused 是合法状态）。"""
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
        path_vars=_PATH_VARS,
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
    all_spans = [s for t in full.turns for s in t.spans]
    tools = [t for t in all_spans if t.kind == SpanKind.TOOL]
    assert len(tools) == 1
    assert tools[0].attributes[ATTR_TOOL_NAME] == "wait_io"
    assert tools[0].status == "ok"
    await _shutdown(task)


# ----------------------------------------------------------------------
# L1 compress span（tool result 超 4000 字符触发 L1 截断）
# ----------------------------------------------------------------------

async def test_engine_records_l1_compress_span_on_long_tool_output(tmp_path: Path):
    """bash 输出超 4000 字符 → L1 截断 → 同时记 tool span + compress:L1 span。"""
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
    by_kind = {s.kind: s for s in all_spans}
    assert SpanKind.TOOL in by_kind
    assert SpanKind.COMPRESS in by_kind
    comp = by_kind[SpanKind.COMPRESS]
    assert comp.attributes[ATTR_LOOP_COMPRESS_LEVEL] == "L1"
    assert comp.attributes[ATTR_LOOP_COMPRESS_FOLDED] == 1
    assert isinstance(comp.attributes[ATTR_LOOP_COMPRESS_BUDGETS], list)
    assert len(comp.attributes[ATTR_LOOP_COMPRESS_BUDGETS]) == 1
    assert comp.attributes[ATTR_LOOP_COMPRESS_BUDGETS][0]  # 非空 hex id
    # tool span 的 result 应该已经 truncated=True
    tool_result = json.loads(by_kind[SpanKind.TOOL].attributes[ATTR_TOOL_RESULT])
    assert tool_result["truncated"] is True
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
    compresses = [s for s in all_spans if s.kind == SpanKind.COMPRESS]
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
    l2 = [s for s in all_spans if s.kind == SpanKind.COMPRESS and s.attributes.get(ATTR_LOOP_COMPRESS_LEVEL) == "L2"]
    assert len(l2) == 1, [str(s.kind) + "/" + str(s.attributes.get(ATTR_LOOP_COMPRESS_LEVEL)) for s in all_spans]
    assert l2[0].attributes[ATTR_LOOP_COMPRESS_FOLDED] > 0
    assert "folded history summary" in l2[0].attributes[ATTR_LOOP_COMPRESS_SUMMARY]
    # 验证 LLM 确实被调用了 2 次（一次 summary，一次 chat）
    assert llm.n == 2
    await _shutdown(task)


# ----------------------------------------------------------------------
# #3 reasoning_content 拼接到 span
# ----------------------------------------------------------------------

async def test_reasoning_span_records_reasoning_content(tmp_path: Path):
    """LLM stream yield reasoning_content_delta → reasoning span 拼完整内容（OTel 字段）。"""

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
    reasoning = [s for t in full.turns for s in t.spans if s.kind == SpanKind.REASONING]
    assert len(reasoning) == 1
    r = reasoning[0]
    assert r.attributes[ATTR_GENAI_RESPONSE_REASONING] == "让我想想... 该用 grep"
    assert r.attributes[ATTR_GENAI_USAGE_CACHED_TOKENS] == 8
    await _shutdown(task)


# ----------------------------------------------------------------------
# #4 tool.execute 异常路径：自定义 tool 直接 raise 也保证记 tool span
# ----------------------------------------------------------------------

async def test_engine_records_tool_span_when_tool_raises(tmp_path: Path):
    """tool.execute 直接 raise（非返回 status=error 的 ToolResult）也保证记一条 TOOL span。"""

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
    all_spans = [s for t in full.turns for s in t.spans]
    tools = [s for s in all_spans if s.kind == SpanKind.TOOL]
    assert len(tools) == 1
    assert tools[0].attributes[ATTR_TOOL_NAME] == "boom"
    assert tools[0].status == "error"
    parsed = json.loads(tools[0].attributes[ATTR_TOOL_RESULT])
    assert "RuntimeError" in parsed["stderr"]
    assert "kaboom" in parsed["stderr"]
    await _shutdown(task)


# ----------------------------------------------------------------------
# #5 父 ID 关系：tool 挂在 act 下，act / reasoning / compress 挂在 turn 下
# ----------------------------------------------------------------------

async def test_span_parent_id_relations(tmp_path: Path):
    """parent_id 关系：
    - TURN 容器：parent_id = loop_span_id（不开新 file 是 loop 不开；本测试不开 trace 都不行）
    - ACT 容器：parent_id = turn_span_id
    - TOOL span：parent_id = act_span_id
    - REASONING / COMPRESS span：parent_id = turn_span_id

    简化：本测试只跑 tool 路径，验证 TOOL → ACT → TURN 三层关系。
    """
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
        by_kind = {s.kind: s for s in turn.spans}
        # TURN 容器必须存在
        assert SpanKind.TURN in by_kind, [s.kind for s in turn.spans]
        turn_span_id = by_kind[SpanKind.TURN].span_id
        # ACT 挂在 TURN 下
        if SpanKind.ACT in by_kind:
            assert by_kind[SpanKind.ACT].parent_id == turn_span_id
            # TOOL 挂在 ACT 下
            for s in turn.spans:
                if s.kind == SpanKind.TOOL:
                    assert s.parent_id == by_kind[SpanKind.ACT].span_id
        # REASONING / COMPRESS 直接挂在 TURN 下
        for s in turn.spans:
            if s.kind in (SpanKind.REASONING, SpanKind.COMPRESS):
                assert s.parent_id == turn_span_id
    await _shutdown(task)
