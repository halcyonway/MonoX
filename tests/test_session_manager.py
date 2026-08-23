"""core/session_manager.py 单测。

- lazy create：dispatch_inbound 收到新 session_key 时才构造 SessionLoop
- dispatch 已有 session_key → 直接 put（不重建）
- idle sweeper：注入 fake clock 验证 destroy 触发
- 销毁后新 dispatch → 重建 + 从 checkpoint 恢复
- per-session checkpoint 文件隔离
- outbound_register 回调在 create 时被真正 await
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.memory import FsMemoryStore
from core.protocol import InboundEvent
from core.session_manager import SessionManager


def _make_session_manager(
    tmp: Path,
    *,
    time_fn,
    idle_timeout_sec: float = 300.0,
    sweep_interval_sec: float = 30.0,
    outbound_register=None,
    outbound_unregister=None,
) -> SessionManager:
    """构造 SessionManager。LoopEngine 用真实例（mock LLM 字段）但其 run() 任务会被
    测试显式 cancel——这样 input_q 的内容稳定可观察。
    """
    llm = MagicMock()
    compression_llm = MagicMock()
    tools = MagicMock()
    compression = MagicMock()
    memory = FsMemoryStore(tmp / "mem")
    return SessionManager(
        llm=llm,
        compression_llm=compression_llm,
        tools=tools,
        compression=compression,
        memory=memory,
        state_root=tmp / "state",
        traces_root=tmp / "traces",
        system_prompt="test",
        skill_summary="",
        outbound_register=outbound_register,
        outbound_unregister=outbound_unregister,
        time_fn=time_fn,
        idle_timeout_sec=idle_timeout_sec,
        sweep_interval_sec=sweep_interval_sec,
    )


async def _stop_engine(sl) -> None:
    """取消 SessionLoop 的 loop task，让 input_q 不再被消费——便于测试观察队列内容。"""
    if sl.task is not None and not sl.task.done():
        sl.task.cancel()
        try:
            await sl.task
        except (asyncio.CancelledError, Exception):
            pass


class TestDispatchInbound:
    @pytest.mark.asyncio
    async def test_lazy_create_on_first_inbound(self):
        tmp = Path("/tmp/test_sm_lazy"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
        sm = _make_session_manager(tmp, time_fn=lambda: 1000.0)
        await sm.start()
        try:
            assert sm.active_sessions() == []
            await sm.dispatch_inbound(InboundEvent(
                session_key="s1", kind="message", text="hi", source="monodesk",
            ))
            assert "s1" in sm.active_sessions()
            sl = sm._sessions["s1"]
            await _stop_engine(sl)
            ev = await asyncio.wait_for(sl.input_q.get(), timeout=1.0)
            assert ev.text == "hi"
        finally:
            await sm.stop()

    @pytest.mark.asyncio
    async def test_reuses_existing_session(self):
        tmp = Path("/tmp/test_sm_reuse"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
        sm = _make_session_manager(tmp, time_fn=lambda: 1000.0)
        await sm.start()
        try:
            await sm.dispatch_inbound(InboundEvent(session_key="s1", kind="message", text="first", source="a"))
            sl = sm._sessions["s1"]
            await sm.dispatch_inbound(InboundEvent(session_key="s1", kind="message", text="second", source="b"))
            assert sm._sessions["s1"] is sl
            await _stop_engine(sl)
            ev1 = await asyncio.wait_for(sl.input_q.get(), timeout=1.0)
            ev2 = await asyncio.wait_for(sl.input_q.get(), timeout=1.0)
            assert (ev1.text, ev2.text) == ("first", "second")
        finally:
            await sm.stop()

    @pytest.mark.asyncio
    async def test_separate_session_keys_get_independent_loops(self):
        tmp = Path("/tmp/test_sm_sep"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
        sm = _make_session_manager(tmp, time_fn=lambda: 1000.0)
        await sm.start()
        try:
            await sm.dispatch_inbound(InboundEvent(session_key="s1", kind="message", text="x", source="a"))
            await sm.dispatch_inbound(InboundEvent(session_key="s2", kind="message", text="y", source="a"))
            assert sm._sessions["s1"] is not sm._sessions["s2"]
            assert sm._sessions["s1"].input_q is not sm._sessions["s2"].input_q
        finally:
            await sm.stop()


class TestIdleSweep:
    @pytest.mark.asyncio
    async def test_session_destroyed_after_idle_timeout(self):
        """fake clock 推进超过 idle_timeout → destroy + 从 dict 移除。"""
        tmp = Path("/tmp/test_sm_idle"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
        clock = {"now": 1000.0}
        sm = _make_session_manager(
            tmp, time_fn=lambda: clock["now"],
            idle_timeout_sec=10.0, sweep_interval_sec=1.0,
        )
        await sm.start()
        try:
            await sm.dispatch_inbound(InboundEvent(session_key="s1", kind="message", text="hi", source="a"))
            sl = sm._sessions["s1"]
            assert sl.task is not None

            # 推进 clock 到 idle 阈值内 → 不应 destroy
            clock["now"] = 1005.0
            await asyncio.sleep(0.05)
            assert "s1" in sm._sessions

            # 推进到超过阈值 + 让 sweeper 跑一轮
            clock["now"] = 1015.0
            await asyncio.sleep(1.2)
            assert "s1" not in sm._sessions, "session should be destroyed after idle timeout"
        finally:
            await sm.stop()

    @pytest.mark.asyncio
    async def test_recreate_after_destroy(self):
        """idle destroy 后新 dispatch → 重建（同一 checkpoint 路径）。"""
        tmp = Path("/tmp/test_sm_recreate"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
        clock = {"now": 1000.0}
        sm = _make_session_manager(
            tmp, time_fn=lambda: clock["now"],
            idle_timeout_sec=10.0, sweep_interval_sec=1.0,
        )
        await sm.start()
        try:
            await sm.dispatch_inbound(InboundEvent(session_key="s1", kind="message", text="first", source="a"))
            sl1 = sm._sessions["s1"]
            sl1_path = sl1.loop_engine._checkpoint._path  # type: ignore[attr-defined]
            assert sl1_path.parent.name == "s1"
            assert sl1_path.name == "checkpoint.jsonl"

            # 推过 idle 阈值
            clock["now"] = 1020.0
            await asyncio.sleep(1.2)
            assert "s1" not in sm._sessions

            # 新 dispatch → 新 SessionLoop 但仍用同一 checkpoint 路径
            await sm.dispatch_inbound(InboundEvent(session_key="s1", kind="message", text="second", source="a"))
            sl2 = sm._sessions["s1"]
            assert sl2 is not sl1
            assert sl2.loop_engine._checkpoint._path == sl1_path  # type: ignore[attr-defined]
        finally:
            await sm.stop()


class TestPerSessionCheckpointIsolation:
    @pytest.mark.asyncio
    async def test_different_sessions_use_different_checkpoint_files(self):
        tmp = Path("/tmp/test_sm_ckpt"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
        sm = _make_session_manager(tmp, time_fn=lambda: 1000.0)
        await sm.start()
        try:
            await sm.dispatch_inbound(InboundEvent(session_key="alpha", kind="message", text="x", source="a"))
            await sm.dispatch_inbound(InboundEvent(session_key="beta", kind="message", text="y", source="a"))
            sl_a = sm._sessions["alpha"]
            sl_b = sm._sessions["beta"]
            path_a = sl_a.loop_engine._checkpoint._path  # type: ignore[attr-defined]
            path_b = sl_b.loop_engine._checkpoint._path  # type: ignore[attr-defined]
            assert path_a != path_b
            assert path_a.parent.name == "alpha"
            assert path_b.parent.name == "beta"
        finally:
            await sm.stop()


class TestOutboundRegisterCallback:
    @pytest.mark.asyncio
    async def test_create_awaits_outbound_register_with_output_q(self):
        tmp = Path("/tmp/test_sm_reg"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
        registered: list[tuple[str, Any]] = []
        unregistered: list[str] = []

        async def _reg(sk, q):
            registered.append((sk, q))

        async def _unreg(sk):
            unregistered.append(sk)

        sm = _make_session_manager(
            tmp, time_fn=lambda: 1000.0,
            outbound_register=_reg, outbound_unregister=_unreg,
        )
        await sm.start()
        try:
            await sm.dispatch_inbound(InboundEvent(session_key="s1", kind="message", text="x", source="a"))
            assert len(registered) == 1
            sk, q = registered[0]
            assert sk == "s1"
            assert q is sm._sessions["s1"].output_q
        finally:
            await sm.stop()
        # sm.stop() 会 destroy 所有 sessions → 触发 unregister
        assert unregistered == ["s1"]


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_destroys_all_sessions(self):
        tmp = Path("/tmp/test_sm_stop"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
        sm = _make_session_manager(tmp, time_fn=lambda: 1000.0)
        await sm.start()
        await sm.dispatch_inbound(InboundEvent(session_key="s1", kind="message", text="x", source="a"))
        await sm.dispatch_inbound(InboundEvent(session_key="s2", kind="message", text="y", source="a"))
        await sm.stop()
        assert sm._sessions == {}
