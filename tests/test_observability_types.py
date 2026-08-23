"""Run / Turn / Span 序列化 round-trip + 一致性。"""
from __future__ import annotations

from core.observability.types import (
    Run,
    Span,
    SpanKind,
    Turn,
    new_run,
    new_turn,
)


def test_round_trip_run():
    run = new_run("default", "你好")
    t = new_turn(0)
    span = Span.now_span(
        SpanKind.REASONING,
        name="reasoning:m1",
        attributes={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
    )
    t2 = Turn(turn_id=t.turn_id, turn_idx=t.turn_idx, spans=tuple([span.close()]))
    run2 = Run(
        run_id=run.run_id,
        session_key=run.session_key,
        user_text=run.user_text,
        final_text="hello back",
        start_ts=run.start_ts,
        end_ts=run.start_ts + 1.5,
        status="ok",
        turns=(t2,),
    )
    d = run2.to_dict()
    back = Run.from_dict(d)
    assert back.run_id == run2.run_id
    assert back.session_key == "default"
    assert back.user_text == "你好"
    assert back.final_text == "hello back"
    assert back.status == "ok"
    assert len(back.turns) == 1
    assert back.turns[0].turn_idx == 0
    assert len(back.turns[0].spans) == 1
    sp = back.turns[0].spans[0]
    assert sp.kind == SpanKind.REASONING
    assert sp.attributes["model"] == "m1"
    assert sp.attributes["messages"][0]["content"] == "hi"


def test_span_close_stamps_end_ts():
    s = Span.now_span(SpanKind.ACT, "act:bash")
    assert s.end_ts is None
    s2 = s.close()
    assert s2.end_ts is not None
    assert s2.span_id == s.span_id


def test_span_status_default_ok():
    s = Span.now_span(SpanKind.COMPRESS, "compress:L1")
    assert s.status == "ok"
    s2 = s.close(status="error")
    assert s2.status == "error"


def test_run_default_status_running():
    r = new_run("k", "x")
    assert r.status == "running"
    assert r.turns == ()


def test_turn_default_empty_spans():
    t = new_turn(0)
    assert t.spans == ()
    assert t.turn_idx == 0