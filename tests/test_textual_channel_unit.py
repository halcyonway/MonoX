"""TextualChannel 单元测试（入站 listen 逻辑）。"""
from __future__ import annotations

import asyncio
import time

import pytest

from core.protocol import InboundEvent
from extensions.channels.textual_chat import TextualChannel


class TestTextualChannelListen:
    @pytest.fixture
    def ch(self) -> TextualChannel:
        return TextualChannel(session_key="test", debug=False)

    async def test_inbound_has_source_textual(self, ch: TextualChannel):
        """TextualChannel 入站事件的 source 应该是 textual。"""
        received_events: list[InboundEvent] = []

        async def drain():
            async for ev in ch.listen():
                received_events.append(ev)
                ch._stop.set()

        async def feed():
            await ch._in_q.put(
                InboundEvent(
                    session_key="test",
                    kind="message",
                    text="hello",
                    source="textual",
                    event_type="user-input",
                    timestamp=time.time(),
                )
            )

        await asyncio.gather(drain(), feed())
        assert len(received_events) == 1
        assert received_events[0].source == "textual"
        assert received_events[0].event_type == "user-input"

    async def test_meta_preserved(self, ch: TextualChannel):
        """meta 字段应该透传到 listen() 返回的事件。"""
        received_events: list[InboundEvent] = []

        async def drain():
            async for ev in ch.listen():
                received_events.append(ev)
                ch._stop.set()

        async def feed():
            await ch._in_q.put(
                InboundEvent(
                    session_key="test",
                    kind="message",
                    text="hello",
                    source="textual",
                    event_type="user-input",
                    timestamp=time.time(),
                    meta={"foo": "bar"},
                )
            )

        await asyncio.gather(drain(), feed())
        assert received_events[0].meta.get("foo") == "bar"

    async def test_multiple_events_fifo(self, ch: TextualChannel):
        """多个事件应该按 FIFO 顺序被消费。"""
        received: list[InboundEvent] = []

        async def drain():
            async for ev in ch.listen():
                received.append(ev)
                if len(received) >= 3:
                    ch._stop.set()

        async def feed():
            for i in range(3):
                await ch._in_q.put(
                    InboundEvent(
                        session_key=f"s{i}",
                        kind="message",
                        text=f"msg{i}",
                        source="textual",
                        event_type="user-input",
                        timestamp=time.time(),
                    )
                )

        await asyncio.gather(drain(), feed())
        assert [ev.text for ev in received] == ["msg0", "msg1", "msg2"]

    async def test_stop_exits_listen(self, ch: TextualChannel):
        """stop() 设置后 listen() 应立即退出。"""
        ch._stop.set()
        count = 0
        async for _ in ch.listen():
            count += 1
        assert count == 0

    async def test_session_key_from_event(self, ch: TextualChannel):
        """session_key 来自事件本身，不是 channel。"""
        received: list[InboundEvent] = []

        async def drain():
            async for ev in ch.listen():
                received.append(ev)
                ch._stop.set()

        async def feed():
            await ch._in_q.put(
                InboundEvent(
                    session_key="custom_session",
                    kind="message",
                    text="hello",
                    source="textual",
                    event_type="user-input",
                    timestamp=time.time(),
                )
            )

        await asyncio.gather(drain(), feed())
        assert received[0].session_key == "custom_session"
