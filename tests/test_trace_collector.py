"""TraceCollector 行为：begin_run / turn / span 嵌套 → end_run flush 到 store。"""
from __future__ import annotations

from pathlib import Path

from core.observability.collector import TraceCollector
from core.observability.jsonl_store import JsonlTraceStore


async def test_begin_end_run_persists(tmp_path: Path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    c = TraceCollector(store, "default")

    run_id = await c.begin_run("hi")
    assert run_id.startswith("t_")
    assert c.current_run_id == run_id

    turn_id = await c.begin_turn(0)
    assert turn_id.startswith("u_")

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
    # 持久化的 run 内有 turn + span
    full = await store.get_run("default", run_id)
    assert full is not None
    assert len(full.turns) == 1
    spans = full.turns[0].spans
    assert len(spans) == 1
    s = spans[0]
    assert s.attributes["model"] == "m1"
    assert s.attributes["messages"][0]["content"] == "hi"
    assert s.attributes["response_text"] == "hello"
    assert s.attributes["latency_ms"] == 100


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


async def test_act_span_records_args_and_result(tmp_path: Path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    c = TraceCollector(store, "default")
    await c.begin_run("hi")
    turn_id = await c.begin_turn(0)
    await c.record_act_span(
        turn_id,
        tool_name="bash",
        args={"command": "ls"},
        result={"stdout": "a\nb", "stderr": "", "exit_code": 0},
        latency_ms=42,
    )
    await c.end_run("done", status="ok")
    runs = await store.list_runs("default")
    full = await store.get_run("default", runs[0].run_id)
    sp = full.turns[0].spans[0]
    assert sp.attributes["tool_name"] == "bash"
    assert sp.attributes["args"] == {"command": "ls"}
    assert sp.attributes["result"]["exit_code"] == 0


async def test_compress_span(tmp_path: Path):
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
    sp = full.turns[0].spans[0]
    assert sp.attributes["level"] == "L1"
    assert sp.attributes["folded_count"] == 3
    assert sp.attributes["budget_ids"] == ["bid_1", "bid_2"]


async def test_end_run_without_begin_is_noop(tmp_path: Path):
    """end_run 在没 begin_run 时不报错。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    c = TraceCollector(store, "default")
    await c.end_run("x", status="ok")
    runs = await store.list_runs("default")
    assert runs == []


async def test_add_span_to_unknown_turn_warns_but_safe(tmp_path: Path):
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