"""JsonlTraceStore: save / get / list / restore 顺序 + 并发 + 文件不存在 graceful。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from core.observability.jsonl_store import JsonlTraceStore
from core.observability.types import Span, SpanKind, new_run, new_turn


def _make_run(run_id: str, ts: float, session_key: str = "default", text: str = "x"):
    run = new_run(session_key, text)
    return Run_with_id(run, run_id, ts)


def Run_with_id(run, run_id, ts):
    """手工替换 run_id / start_ts（new_run 用 uuid 不可控）。"""
    from core.observability.types import Run
    return Run(
        run_id=run_id,
        session_key=run.session_key,
        user_text=run.user_text,
        final_text=run.final_text,
        start_ts=ts,
        end_ts=ts + 0.5,
        status="ok",
        turns=run.turns,
    )


async def test_save_and_get(tmp_path: Path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    run = Run_with_id(new_run("default", "hi"), "t_aaa", 1000.0)
    await store.save_run(run)
    got = await store.get_run("default", "t_aaa")
    assert got is not None
    assert got.run_id == "t_aaa"
    assert got.user_text == "hi"
    assert got.status == "ok"


async def test_upsert_overwrites_same_run_id(tmp_path: Path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    r1 = Run_with_id(new_run("default", "hi"), "t_a", 1000.0)
    r2 = Run_with_id(new_run("default", "hi"), "t_a", 1000.0)
    r2 = r2.__class__(
        run_id=r2.run_id, session_key=r2.session_key, user_text=r2.user_text,
        final_text="done", start_ts=r2.start_ts, end_ts=r2.end_ts, status="ok",
        turns=(),
    )
    await store.save_run(r1)
    await store.save_run(r2)
    got = await store.get_run("default", "t_a")
    assert got is not None
    assert got.final_text == "done"
    # 文件不应有重复 run_id
    text = (tmp_path / "traces.jsonl").read_text()
    assert text.count('"run_id": "t_a"') == 1


async def test_list_runs_orders_by_start_desc(tmp_path: Path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    for i, ts in enumerate([1.0, 3.0, 2.0]):
        r = Run_with_id(new_run("default", f"text-{i}"), f"t_{i}", ts)
        await store.save_run(r)
    runs = await store.list_runs("default", limit=10)
    assert [r.run_id for r in runs] == ["t_1", "t_2", "t_0"]


async def test_get_run_missing_returns_none(tmp_path: Path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    assert await store.get_run("default", "nope") is None


async def test_list_runs_missing_file_graceful(tmp_path: Path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    runs = await store.list_runs("default")
    assert runs == []


async def test_restore_returns_full_runs(tmp_path: Path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    t = new_turn(0)
    span = Span.now_span(SpanKind.REASONING, "reasoning:m1").close()
    t2 = t.__class__(turn_id=t.turn_id, turn_idx=t.turn_idx, spans=(span,))
    r = Run_with_id(new_run("default", "hi"), "t_aa", 1.0)
    r = r.__class__(
        run_id=r.run_id, session_key=r.session_key, user_text=r.user_text,
        final_text=r.final_text, start_ts=r.start_ts, end_ts=r.end_ts,
        status=r.status, turns=(t2,),
    )
    await store.save_run(r)
    rs = await store.restore("default", limit=10)
    assert len(rs) == 1
    assert rs[0].turns[0].spans[0].kind == SpanKind.REASONING


async def test_concurrent_saves_safe(tmp_path: Path):
    """同一 store 并发 save_run 不会损坏文件。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl")

    async def save(i: int):
        r = Run_with_id(new_run("default", f"text-{i}"), f"t_{i}", float(i))
        await store.save_run(r)

    await asyncio.gather(*[save(i) for i in range(20)])
    runs = await store.list_runs("default", limit=100)
    assert len(runs) == 20
    assert {r.run_id for r in runs} == {f"t_{i}" for i in range(20)}


async def test_max_file_bytes_trims(tmp_path: Path):
    """超 max_file_bytes 时从头裁掉旧 run。"""
    store = JsonlTraceStore(tmp_path / "traces.jsonl", max_file_bytes=2048)
    # 写大 run 撑爆文件
    big_attrs = {"messages": [{"role": "user", "content": "x" * 600}]}
    for i in range(20):
        r = Run_with_id(new_run("default", f"t{i}"), f"id_{i}", float(i))
        r = r.__class__(
            run_id=r.run_id, session_key=r.session_key, user_text=r.user_text,
            final_text=r.final_text, start_ts=r.start_ts, end_ts=r.end_ts,
            status=r.status, turns=(),
        )
        # 用 attributes 假装多塞点数据 —— 不影响 jsonl 文件 size 因为 attributes
        # 不在 Run 上，这里改 max_file_bytes 触发裁剪用大 user_text
        r = r.__class__(
            run_id=r.run_id, session_key=r.session_key,
            user_text="x" * 600, final_text=r.final_text,
            start_ts=r.start_ts, end_ts=r.end_ts, status=r.status,
            turns=(),
        )
        await store.save_run(r)
    # 文件大小被裁剪
    assert (tmp_path / "traces.jsonl").stat().st_size <= 2048 + 200
    # 旧 run 已被裁掉，新 run 还在
    runs = await store.list_runs("default", limit=100)
    assert any(r.run_id == "id_19" for r in runs)
    assert not any(r.run_id == "id_0" for r in runs)