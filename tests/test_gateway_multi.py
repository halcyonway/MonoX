"""MultiChannelGateway 单元测试。"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.channel.base import Channel
from core.event_wrapper import parse_output
from core.gateway.multi import MultiChannelGateway
from core.protocol import FinalMessage, InboundEvent, StreamEvent


class MockChannel(Channel):
    def __init__(self, name: str = "mock"):
        self.name = name
        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._sent: list[StreamEvent] = []
        self._started = False
        self._stopped = False

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._stopped = True

    def inject(self, ev: InboundEvent) -> None:
        self._queue.put_nowait(ev)

    async def listen(self) -> AsyncIterator[InboundEvent]:
        while True:
            try:
                ev = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            yield ev

    async def send(self, ev: StreamEvent) -> None:
        self._sent.append(ev)

    def sent_texts(self) -> list[str]:
        return [e.text for e in self._sent if isinstance(e, FinalMessage)]


# ------------------------------------------------------------------
# parse_output 直接测试
# ------------------------------------------------------------------

class TestParseOutput:
    def test_single_send_tag(self):
        routes = parse_output('<send channel="feishu">hello</send>', "terminal")
        assert routes == [("feishu", "hello")]

    def test_multiple_send_tags(self):
        out = '<send channel="a">A</send><send channel="b">B</send>'
        routes = parse_output(out, "default")
        assert routes == [("a", "A"), ("b", "B")]

    def test_no_send_tag_uses_pending(self):
        routes = parse_output("plain text", "feishu")
        assert routes == [("feishu", "plain text")]

    def test_unknown_channel_preserved(self):
        out = '<send channel="slack">msg</send>'
        routes = parse_output(out, "feishu")
        assert routes == [("slack", "msg")]


# ------------------------------------------------------------------
# MultiChannelGateway fan-in
# ------------------------------------------------------------------

class TestMultiChannelGatewayFanIn:
    @pytest.mark.asyncio
    async def test_pending_channel_dict_works(self):
        """_pending_channel 字典正确映射 session_key → channel name。"""
        ch = MockChannel("feishu")
        input_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
        output_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
        gw = MultiChannelGateway(
            [("feishu", ch)],
            loop_input=input_q,
            loop_output=output_q,
            default_channel="terminal",
        )

        # 模拟 pump_inbound 设置 pending_channel 的逻辑
        ev = InboundEvent(
            session_key="oc_chat1",
            kind="message",
            text="hello",
            source="feishu",
            event_type="user-input",
            timestamp=time.time(),
        )
        gw._pending_channel[ev.session_key] = ev.source

        assert gw._pending_channel.get("oc_chat1") == "feishu"

    @pytest.mark.asyncio
    async def test_multi_listen_merges_events(self):
        """两个 channel 的事件应该被合并。"""
        ch1 = MockChannel("ch1")
        ch2 = MockChannel("ch2")
        input_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
        output_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
        gw = MultiChannelGateway(
            [("ch1", ch1), ("ch2", ch2)],
            loop_input=input_q,
            loop_output=output_q,
        )

        collected: list[InboundEvent] = []

        async def pump():
            async for ev in gw._multi_listen():
                collected.append(ev)
                if len(collected) >= 4:
                    return

        t = asyncio.create_task(pump())
        await asyncio.sleep(0.05)

        ch1.inject(InboundEvent(
            session_key="s1", kind="message", text="a",
            source="ch1", event_type="user-input", timestamp=time.time(),
        ))
        ch2.inject(InboundEvent(
            session_key="s2", kind="message", text="b",
            source="ch2", event_type="user-input", timestamp=time.time(),
        ))
        ch1.inject(InboundEvent(
            session_key="s1", kind="message", text="c",
            source="ch1", event_type="user-input", timestamp=time.time(),
        ))
        ch2.inject(InboundEvent(
            session_key="s2", kind="message", text="d",
            source="ch2", event_type="user-input", timestamp=time.time(),
        ))

        await t
        assert len(collected) == 4
        texts = {ev.text for ev in collected}
        assert texts == {"a", "b", "c", "d"}


# ------------------------------------------------------------------
# MultiChannelGateway fan-out
# ------------------------------------------------------------------

class TestMultiChannelGatewayFanOut:
    @pytest.mark.asyncio
    async def test_route_to_specific_channel(self):
        """带 <send channel="feishu"> 的消息只发到 feishu channel。"""
        feishu_ch = MockChannel("feishu")
        terminal_ch = MockChannel("terminal")
        input_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
        output_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
        gw = MultiChannelGateway(
            [("feishu", feishu_ch), ("terminal", terminal_ch)],
            loop_input=input_q,
            loop_output=output_q,
            default_channel="terminal",
        )

        # 直接调用 route_event（不用 pump_outbound 的循环）
        msg = FinalMessage(text='<send channel="feishu">hello feishu</send>')
        await gw._route_event(msg)

        assert "hello feishu" in feishu_ch.sent_texts()
        assert "hello feishu" not in terminal_ch.sent_texts()

    @pytest.mark.asyncio
    async def test_no_send_uses_default_channel(self):
        """无 send 标签且无 session_key 时发到 default_channel。"""
        feishu_ch = MockChannel("feishu")
        terminal_ch = MockChannel("terminal")
        input_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
        output_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
        gw = MultiChannelGateway(
            [("feishu", feishu_ch), ("terminal", terminal_ch)],
            loop_input=input_q,
            loop_output=output_q,
            default_channel="terminal",
        )

        # FinalMessage 没有 session_key（getattr 返回 None），走 default_channel
        msg = FinalMessage(text="plain response")
        await gw._route_event(msg)

        assert "plain response" in terminal_ch.sent_texts()
        assert "plain response" not in feishu_ch.sent_texts()

    @pytest.mark.asyncio
    async def test_multiple_channels_parallel(self):
        """多条消息并行路由到不同 channel。"""
        feishu_ch = MockChannel("feishu")
        terminal_ch = MockChannel("terminal")
        input_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
        output_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
        gw = MultiChannelGateway(
            [("feishu", feishu_ch), ("terminal", terminal_ch)],
            loop_input=input_q,
            loop_output=output_q,
            default_channel="terminal",
        )

        await asyncio.gather(
            gw._route_event(FinalMessage(text='<send channel="feishu">f1</send>')),
            gw._route_event(FinalMessage(text='<send channel="terminal">t1</send>')),
            gw._route_event(FinalMessage(text='<send channel="feishu">f2</send>')),
        )

        assert set(feishu_ch.sent_texts()) == {"f1", "f2"}
        assert terminal_ch.sent_texts() == ["t1"]


# ------------------------------------------------------------------
# broadcast
# ------------------------------------------------------------------

class TestBroadcast:
    @pytest.mark.asyncio
    async def test_broadcast_to_all_channels(self):
        """非文本事件广播到所有 channel。"""
        ch1 = MockChannel("ch1")
        ch2 = MockChannel("ch2")
        input_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
        output_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
        gw = MultiChannelGateway(
            [("ch1", ch1), ("ch2", ch2)],
            loop_input=input_q,
            loop_output=output_q,
        )

        from core.protocol import StatusChange
        await gw._broadcast(StatusChange(state="thinking"))

        # broadcast 发的是同一个 StreamEvent 对象
        assert len(ch1._sent) == 1
        assert len(ch2._sent) == 1
        assert isinstance(ch1._sent[0], StatusChange)
