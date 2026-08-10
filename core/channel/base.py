"""Channel adapter 接口契约。

所有 IM adapter（飞书 / Slack / Terminal）实现此协议。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from core.protocol import InboundEvent, StreamEvent


class Channel(Protocol):
    """双向桥接：
    - listen() 输出 InboundEvent（IM 输入 → core）
    - send() 接收 StreamEvent（core → IM 输出）
    """

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def listen(self) -> AsyncIterator[InboundEvent]: ...
    async def send(self, event: StreamEvent) -> None: ...