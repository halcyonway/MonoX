"""TraceCollector 行为：begin_run / turn / span 嵌套 → end_run flush 到 store。

v2 协议：属性键全部走 OTel Semantic Convention；turn container 自己也是一个
kind=turn 的 span（在 turn.spans[0]）。work span（reasoning / tool / compress）
挂 turn_span_id 下。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.observability.collector import TraceCollector
from core.observability.jsonl_store import JsonlTraceStore
from core.observability.otel_attrs import (
    ATTR_GENAI_CLIENT_OPERATION_DURATION,
    ATTR_GENAI_REQUEST_MESSAGES,
    ATTR_GENAI_REQUEST_MODEL,
    ATTR_GENAI_RESPONSE_TEXT,
    ATTR_LOOP_COMPRESS_BUDGETS,
    ATTR_LOOP_COMPRESS_FOLDED,
    ATTR_LOOP_COMPRESS_LEVEL,
    ATTR_LOOP_COMPRESS_SUMMARY,
    ATTR_TOOL_CALL_ARGUMENTS,
    ATTR_TOOL_NAME,
    ATTR_TOOL_RESULT,
)
from core.observability.types import SpanKind


async def test_begin_end_run_persists(tmp_path: Path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    c = TraceCollector(store, "default")

    run_id = await c.begin_run("hi")
    assert run_id.startswith("t_")
    assert c.current_run_id == run_id

    turn_id = await c.begin_turn(0)
    # v2：turn_id == TURN span_id（统一 `s_` 前缀），让 parent_id 链跑通
    assert turn_id.startswith("s_")

    await c.record_llm_span(
        turn_id,
        model="m1",
        messages=[{"role": "user", "content": "hi"}],
        response_text="hello",
        reasoning_content=None,
        usage={"prompt_tokens": 5, "completion_tokens": 2},
        finish_reason="stop",
        latency_ms=100,
    )

    await c.end_run("hello", status="ok")
    assert c.current_run_id is not None  # 还在内存里直到下次 begin_run 才清
    # 但 store 已 flush
    runs = await store.list_runs("default")
    assert len(runs) == 1
    assert runs[0].user_text == "hi"
    assert runs[0].status == "ok"
    assert runs[0].schema_version == 2
    # 持久化的 run 内有 turn + span
    full = await store.get_run("default", run_id)
    assert full is not None
    assert len(full.turns) == 1
    # spans[0] 是 TURN 容器 span，spans[1] 才是 reasoning
    turn_spans = full.turns[0].spans
    assert len(turn_spans) == 2
    assert turn_spans[0].kind == SpanKind.TURN
    assert turn_spans[1].kind == SpanKind.REASONING
    s = turn_spans[1]
    assert s.attributes[ATTR_GENAI_REQUEST_MODEL] == "m1"
    assert s.attributes[ATTR_GENAI_REQUEST_MESSAGES][0]["content"] == "hi"
    assert s.attributes[ATTR_GENAI_RESPONSE_TEXT] == "hello"
    assert s.attributes[ATTR_GENAI_CLIENT_OPERATION_DURATION] == 100


async def test_two_runs_overwrite_idempotent(tmp_path: Path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    c = TraceCollector(store, "default")

    run_id = await c.begin_run("hi")
    await c.begin_turn(0)
    await c.end_run("first", status="ok")
    # 第二次 begin_run 起新 run（旧 run 应已 flush 到 store）
    run_id_2 = await c.begin_run("hi2")
    assert run_id_2 != run_id
    await c.begin_turn(0)
    await c.end_run("second", status="ok")
    runs = await store.list_runs("default")
    assert len(runs) == 2
    # 按时间倒序
    assert runs[0].user_text == "hi2"
    assert runs[1].user_text == "hi"


async def test_tool_span_records_otel_attrs(tmp_path: Path):
    """record_tool_span 走 OTel 字段：tool.name / tool.call.arguments / tool.result。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    c = TraceCollector(store, "default")
    await c.begin_run("hi")
    turn_id = await c.begin_turn(0)
    # ACT 容器先行（一个 turn 里 tool 数）
    await c.record_act_span(turn_id, tool_calls_count=1)
    await c.record_tool_span(
        turn_id,
        tool_name="bash",
        call_id="call_abc",
        args={"command": "ls"},
        result={"stdout": "a\nb", "stderr": "", "exit_code": 0},
        latency_ms=42,
    )
    await c.end_run("done", status="ok")
    runs = await store.list_runs("default")
    full = await store.get_run("default", runs[0].run_id)
    # turn.spans: [TURN, ACT, TOOL]
    sps = full.turns[0].spans
    kinds = [s.kind for s in sps]
    assert kinds == [SpanKind.TURN, SpanKind.ACT, SpanKind.TOOL]
    tool_sp = sps[2]
    assert tool_sp.attributes[ATTR_TOOL_NAME] == "bash"
    # OTel 规定 args / result 是 string；MonoX 存 JSON 字符串
    assert json.loads(tool_sp.attributes[ATTR_TOOL_CALL_ARGUMENTS]) == {"command": "ls"}
    parsed_result = json.loads(tool_sp.attributes[ATTR_TOOL_RESULT])
    assert parsed_result["exit_code"] == 0
    # tool 嵌套在 ACT 下
    assert tool_sp.parent_id == sps[1].span_id


async def test_compress_span(tmp_path: Path):
    """record_compress_span 走 loop.compress.* 扩展字段。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    c = TraceCollector(store, "default")
    await c.begin_run("hi")
    turn_id = await c.begin_turn(0)
    await c.record_compress_span(
        turn_id,
        level="L1",
        summary="folded 3 long tool outputs",
        folded_count=3,
        budget_ids=["bid_1", "bid_2"],
    )
    await c.end_run("done", status="ok")
    runs = await store.list_runs("default")
    full = await store.get_run("default", runs[0].run_id)
    sps = full.turns[0].spans
    # spans: [TURN, COMPRESS]
    assert sps[1].kind == SpanKind.COMPRESS
    a = sps[1].attributes
    assert a[ATTR_LOOP_COMPRESS_LEVEL] == "L1"
    assert a[ATTR_LOOP_COMPRESS_SUMMARY] == "folded 3 long tool outputs"
    assert a[ATTR_LOOP_COMPRESS_FOLDED] == 3
    assert a[ATTR_LOOP_COMPRESS_BUDGETS] == ["bid_1", "bid_2"]


async def test_end_run_without_begin_is_noop(tmp_path: Path):
    """end_run 在没 begin_run 时不报错。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    c = TraceCollector(store, "default")
    await c.end_run("x", status="ok")
    runs = await store.list_runs("default")
    assert runs == []


async def test_add_span_to_unknown_turn_warns_but_safe(tmp_path: Path):
    """record_llm_span 在 turn_id 不存在时 warn 但不崩。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    c = TraceCollector(store, "default")
    await c.begin_run("hi")
    # 没 begin_turn，直接 record_llm_span（不存在的 turn_id）
    await c.record_llm_span(
        "u_doesnotexist",
        model="m",
        messages=[],
        response_text="",
        reasoning_content=None,
        usage=None,
        finish_reason=None,
        latency_ms=0,
    )
    # 不应崩；end_run 时 flush 一个无 turn 的 run
    await c.end_run("done", status="ok")
    runs = await store.list_runs("default")
    assert len(runs) == 1
    # run.turns 为空（没建 turn 容器）
    full = await store.get_run("default", runs[0].run_id)
    assert full is not None
    assert full.turns == ()


async def test_phase_spans_and_schema_version(tmp_path: Path):
    """run-level bootstrap/loop/finalize span 能正常 begin/end，schema_version=2。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    c = TraceCollector(store, "default")
    run_id = await c.begin_run("hi")
    bs_id = await c.begin_span(parent_id=run_id, kind=SpanKind.BOOTSTRAP, name="bootstrap")
    await c.end_span(bs_id, status="ok")
    loop_id = await c.begin_span(parent_id=run_id, kind=SpanKind.LOOP, name="loop")
    await c.end_span(loop_id, status="ok")
    fz_id = await c.begin_span(parent_id=run_id, kind=SpanKind.FINALIZE, name="finalize")
    await c.end_span(fz_id, status="ok")
    await c.end_run("done", status="ok")

    full = await store.get_run("default", run_id)
    assert full is not None
    assert full.schema_version == 2
    assert len(full.spans) == 3
    kinds = [s.kind for s in full.spans]
    assert kinds == [SpanKind.BOOTSTRAP, SpanKind.LOOP, SpanKind.FINALIZE]
    # 所有 run-level span 都挂在 run_id 下
    for s in full.spans:
        assert s.parent_id == run_id
        assert s.end_ts is not None
