"""channel 进程 mini-runtime 模板（私有 helper）。

每个 channel 的 `__main__.py` / `run.py` 共享：
- pump_inbound：channel.listen() → RuntimeWSClient.send()
- pump_outbound：RuntimeWSClient.recv() → channel.send()
- main_loop：起 channel + RuntimeWSClient（带 source） + 双 pump gather
- supervisor：包 main_loop，crash 后指数退避重启（防 OSError: Address already in use
  等暂时性错误拖死整个 Runtime）

`hello_source` 由调用方传入——Runtime 用 `(session_key, source)` 索引 ws conn。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.channel.base import Channel
from core.protocol.wire_frames import inbound_to_frame
from core.runtime_ws_client import RuntimeWSClient

_log = logging.getLogger("monox.channel_runtime")


# 指数退避表（秒）。第 N 次崩溃后等 BACKOFF[min(N, len-1)] 秒再起。
SUPERVISOR_BACKOFF = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)


async def pump_inbound(channel: Channel, ws_client: RuntimeWSClient) -> None:
    """channel 上行 → Runtime。inbound 帧来源（user_input / command / interrupt）。"""
    async for ev in channel.listen():
        frame = inbound_to_frame(ev, seq=0)
        if frame is None:
            continue
        try:
            await ws_client.send(frame)
        except Exception as e:
            _log.warning("inbound pump send failed: %s", e)
            return


async def pump_outbound(channel: Channel, ws_client: RuntimeWSClient) -> None:
    """Runtime 下行 → channel。StreamEvent 包含 token / final / error / status / 等。"""
    while True:
        ev = await ws_client.recv()
        try:
            await channel.send(ev)
        except Exception as e:
            _log.warning("outbound pump channel.send failed: %s", e)


async def _run_once(channel: Channel, ws_client: RuntimeWSClient) -> None:
    """单次 main_loop：起 channel + ws client + 双 pump gather；任一异常退出。"""
    await channel.start()
    ws_task = asyncio.create_task(ws_client.run(), name="ws-client")
    try:
        await asyncio.gather(
            pump_inbound(channel, ws_client),
            pump_outbound(channel, ws_client),
        )
    finally:
        await ws_client.stop()
        ws_task.cancel()
        try:
            await ws_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await channel.stop()
        except Exception:
            pass


async def run_channel(channel: Channel, *, ws_client: RuntimeWSClient) -> None:
    """supervisor 包 _run_once：crash 后指数退避重启。

    - 收到 CancelledError（Runtime 整体 shutdown）→ 不重启，直接退出
    - 其他异常 → 记日志 + 按 SUPERVISOR_BACKOFF 退避 + 重跑 _run_once
    - 无限重启直到被取消（适合长期运行的 Runtime 进程）

    设计动机：channel 内部依赖（lark-oapi / textual / websockets.sync.server）偶尔
    会因端口被占 / 暂时性网络错误抛出 OSError；supervisor 防止一次崩溃拖死 Runtime。
    """
    attempt = 0
    while True:
        try:
            await _run_once(channel, ws_client)
            # 正常退出（gather 抛 CancelledError 不会到这）→ 不重启
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            attempt += 1
            delay = SUPERVISOR_BACKOFF[min(attempt - 1, len(SUPERVISOR_BACKOFF) - 1)]
            _log.error(
                "channel crashed (%s: %s); restart in %.1fs (attempt %d)",
                type(e).__name__, e, delay, attempt,
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
