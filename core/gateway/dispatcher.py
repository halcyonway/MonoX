"""Gateway: Channel ↔ Loop 双向桥接。"""
from __future__ import annotations

import asyncio

from core.channel.base import Channel
from core.protocol import InboundEvent, StreamEvent


class Gateway:
    def __init__(
        self,
        channel: Channel,
        *,
        loop_input: asyncio.Queue[InboundEvent],
        loop_output: asyncio.Queue[StreamEvent],
    ) -> None:
        self._channel = channel
        self._loop_input = loop_input
        self._loop_output = loop_output

    async def run(self) -> None:
        await self._channel.start()
        try:
            await asyncio.gather(self._pump_inbound(), self._pump_outbound())
        finally:
            await self._channel.stop()

    async def _pump_inbound(self) -> None:
        async for ev in self._channel.listen():
            await self._loop_input.put(ev)

    async def _pump_outbound(self) -> None:
        while True:
            ev = await self._loop_output.get()
            await self._channel.send(ev)