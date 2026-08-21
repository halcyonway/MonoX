"""MultiChannelGateway: 多 channel fan-in / fan-out 桥接。

fan-in：并行 listen() 所有 channel，事件进入单一 loop_input queue
fan-out：EventWrapper.parse_output() 解析 <send channel="xxx"> 标签，
         路由到对应 channel；无标签则回 pending_channel
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

from core.channel.base import Channel
from core.event_wrapper import parse_output
from core.protocol import InboundEvent, StreamEvent


class MultiChannelGateway:
    def __init__(
        self,
        channels: list[tuple[str, Channel]],
        *,
        loop_input: asyncio.Queue[InboundEvent],
        loop_output: asyncio.Queue[StreamEvent],
        default_channel: str = "terminal",
    ) -> None:
        self._channels = dict(channels)
        self._loop_input = loop_input
        self._loop_output = loop_output
        self._default_channel = default_channel
        self._pending_channel: dict[str, str] = {}

    async def run(self) -> None:
        await asyncio.gather(*[ch.start() for _, ch in self._channels.items()])
        try:
            await asyncio.gather(
                self._pump_inbound(),
                self._pump_outbound(),
            )
        finally:
            await asyncio.gather(*[ch.stop() for _, ch in self._channels.items()])

    # ------------------------------------------------------------------
    # fan-in
    # ------------------------------------------------------------------

    async def _pump_inbound(self) -> None:
        try:
            async for ev in self._multi_listen():
                self._pending_channel[ev.session_key] = ev.source
                await self._loop_input.put(ev)
        except asyncio.CancelledError:
            pass

    async def _multi_listen(self) -> AsyncIterator[InboundEvent]:
        listeners = [
            self._safe_listen(name, ch)
            for name, ch in self._channels.items()
        ]
        async for ev in self._merge(listeners):
            yield ev

    async def _safe_listen(self, name: str, ch: Channel) -> AsyncIterator[InboundEvent]:
        """单 channel listen，异常不打断其他 channel。"""
        try:
            async for ev in ch.listen():
                yield ev
        except asyncio.CancelledError:
            pass
        except Exception as e:
            sys.stderr.write(f"[MultiChannelGateway] channel {name} listen error: {e}\n")
            sys.stderr.flush()

    async def _merge(
        self, iterators: list[AsyncIterator[InboundEvent]]
    ) -> AsyncIterator[InboundEvent]:
        """把多个 AsyncIterator 合并成一个。"""
        queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        pending: set[asyncio.Task] = set()

        async def feeder(it: AsyncIterator[InboundEvent]):
            try:
                async for ev in it:
                    queue.put_nowait(ev)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                sys.stderr.write(f"[MultiChannelGateway] feeder error: {e}\n")
                sys.stderr.flush()
            finally:
                queue.put_nowait(None)  # sentinel

        for i, it in enumerate(iterators):
            task = asyncio.create_task(feeder(it))
            pending.add(task)
            task.add_done_callback(pending.discard)

        done = asyncio.Event()
        pending_count = len(pending)

        async def drain():
            nonlocal pending_count
            while pending_count > 0:
                item = await queue.get()
                if item is None:
                    pending_count -= 1
                else:
                    yield item

        async def close_on_cancel():
            try:
                async for _ in drain():
                    pass
            except asyncio.CancelledError:
                pass

        try:
            async for ev in drain():
                yield ev
        except asyncio.CancelledError:
            for t in pending:
                t.cancel()
            raise

    # ------------------------------------------------------------------
    # fan-out
    # ------------------------------------------------------------------

    async def _pump_outbound(self) -> None:
        while True:
            try:
                ev = await asyncio.wait_for(self._loop_output.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            await self._route_event(ev)

    async def _route_event(self, ev: StreamEvent) -> None:
        from core.protocol import FinalMessage, ErrorEvent

        text = None
        if isinstance(ev, FinalMessage):
            text = ev.text
        elif isinstance(ev, ErrorEvent):
            text = f"❌ {ev.msg}"
        else:
            await self._broadcast(ev)
            return

        session_key = getattr(ev, "session_key", None) or ""
        pending = self._pending_channel.get(session_key, self._default_channel)
        routes = parse_output(text, pending)

        sends = [
            self._send_to_channel(ch_name, content)
            for ch_name, content in routes
            if ch_name in self._channels
        ]
        if sends:
            await asyncio.gather(*sends, return_exceptions=True)

    async def _send_to_channel(self, name: str, content: str) -> None:
        from core.protocol import FinalMessage

        if not content:
            return
        ch = self._channels.get(name)
        if ch is None:
            return
        try:
            await ch.send(FinalMessage(text=content))
        except Exception as e:
            sys.stderr.write(f"[MultiChannelGateway] send to {name} error: {e}\n")
            sys.stderr.flush()

    async def _broadcast(self, ev: StreamEvent) -> None:
        sends = [
            self._safe_send(name, ch, ev)
            for name, ch in self._channels.items()
        ]
        if sends:
            await asyncio.gather(*sends, return_exceptions=True)

    async def _safe_send(self, name: str, ch: Channel, ev: StreamEvent) -> None:
        try:
            await ch.send(ev)
        except Exception as e:
            sys.stderr.write(f"[MultiChannelGateway] broadcast to {name} error: {e}\n")
            sys.stderr.flush()
