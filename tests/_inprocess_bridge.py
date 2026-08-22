"""测试辅助：单 Channel ↔ Loop queue 桥接 helper。

Runtime↔Gateway 进程级解耦后，真实环境下 Channel ↔ Loop 中间隔了 ws。
但在单元 / 集成测试里直接用 queue 桥接更简单——这个 helper 提供这个能力，
**仅用于测试**，不属于 core 稳定 API。

用法：
    ch = MockChannel(...)
    iq, oq = asyncio.Queue(), asyncio.Queue()
    bridge = InProcessBridge(ch, loop_input=iq, loop_output=oq)
    asyncio.create_task(bridge.run())
    asyncio.create_task(loop.run(iq, oq))
"""
from __future__ import annotations

import asyncio

from core.channel.base import Channel
from core.protocol import InboundEvent, StreamEvent


class InProcessBridge:
    """单 Channel ↔ Loop 的 in-process 双向桥接。

    等价于 Runtime + Gateway 的端到端链路（但用 asyncio.Queue 替代 ws）。
    旧 `core.gateway.Gateway` 类的等价物——挪到这里是因为它仅供测试使用。
    """

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