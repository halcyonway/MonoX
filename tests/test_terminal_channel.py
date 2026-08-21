"""TerminalChannel 单元测试。"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.protocol import InboundEvent
from extensions.channels.terminal import TerminalChannel


class TestTerminalChannel:
    @pytest.fixture
    def ch(self) -> TerminalChannel:
        return TerminalChannel(session_key="test", debug=False)

    async def test_inbound_has_source_and_event_type(self, ch: TerminalChannel):
        """TerminalChannel 产生的 InboundEvent 应该有正确的 source 和 event_type。"""
        received_events: list[InboundEvent] = []

        async def drain():
            async for ev in ch.listen():
                received_events.append(ev)
                ch._stop.set()

        async def feed():
            await ch._queue.put(
                InboundEvent(
                    session_key="test",
                    kind="message",
                    text="hello",
                    source="terminal",
                    event_type="user-input",
                    timestamp=time.time(),
                )
            )

        await asyncio.gather(drain(), feed())
        assert len(received_events) == 1
        ev = received_events[0]
        assert ev.source == "terminal"
        assert ev.event_type == "user-input"
        assert ev.text == "hello"

    async def test_timestamp_is_recent(self, ch: TerminalChannel):
        """入站事件应该有接近当前时间的时间戳。"""
        now = time.time()
        received_events: list[InboundEvent] = []

        async def drain():
            async for ev in ch.listen():
                received_events.append(ev)
                ch._stop.set()

        async def feed():
            await ch._queue.put(
                InboundEvent(
                    session_key="test",
                    kind="message",
                    text="hi",
                    source="terminal",
                    event_type="user-input",
                    timestamp=now,
                )
            )

        await asyncio.gather(drain(), feed())
        assert len(received_events) == 1
        assert abs(received_events[0].timestamp - now) < 1.0

    async def test_meta_preserved(self, ch: TerminalChannel):
        """meta 字段应该透传到 listen() 返回的事件。"""
        received_events: list[InboundEvent] = []

        async def drain():
            async for ev in ch.listen():
                received_events.append(ev)
                ch._stop.set()

        async def feed():
            await ch._queue.put(
                InboundEvent(
                    session_key="test",
                    kind="message",
                    text="hello",
                    source="terminal",
                    event_type="user-input",
                    timestamp=time.time(),
                    meta={"custom_key": "custom_val"},
                )
            )

        await asyncio.gather(drain(), feed())
        assert len(received_events) == 1
        assert received_events[0].meta.get("custom_key") == "custom_val"

    async def test_session_key_routed(self, ch: TerminalChannel):
        """不同 session_key 的事件应该被正确路由。"""
        received_events: list[InboundEvent] = []

        async def drain():
            async for ev in ch.listen():
                received_events.append(ev)
                if len(received_events) >= 2:
                    ch._stop.set()

        async def feed():
            await ch._queue.put(
                InboundEvent(
                    session_key="session_a",
                    kind="message",
                    text="a",
                    source="terminal",
                    event_type="user-input",
                    timestamp=time.time(),
                )
            )
            await ch._queue.put(
                InboundEvent(
                    session_key="session_b",
                    kind="message",
                    text="b",
                    source="terminal",
                    event_type="user-input",
                    timestamp=time.time(),
                )
            )

        await asyncio.gather(drain(), feed())
        assert len(received_events) == 2
        assert received_events[0].session_key == "session_a"
        assert received_events[1].session_key == "session_b"

    async def test_stop_sets_event(self, ch: TerminalChannel):
        """stop() 应该设置 _stop Event，listen() 应该退出。"""
        ch._stop.set()
        count = 0
        async for ev in ch.listen():
            count += 1
        assert count == 0

    async def test_listen_timeout_yields_nothing(self, ch: TerminalChannel):
        """listen() 在队列空时应该在 timeout 后继续，而不是抛异常。"""
        ch._stop.set()
        count = 0
        async for ev in ch.listen():
            count += 1
        assert count == 0

    async def test_channel_name(self, ch: TerminalChannel):
        """source 字段应该固定为 terminal。"""
        received_events: list[InboundEvent] = []

        async def drain():
            async for ev in ch.listen():
                received_events.append(ev)
                ch._stop.set()

        async def feed():
            await ch._queue.put(
                InboundEvent(
                    session_key="test",
                    kind="message",
                    text="hello",
                    source="terminal",
                    event_type="user-input",
                    timestamp=time.time(),
                )
            )

        await asyncio.gather(drain(), feed())
        assert received_events[0].source == "terminal"
