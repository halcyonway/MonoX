"""monoDesk channel 进程入口。

启动 monoDesk adapter（监听 `:8766` 给 desktop client 连）+ RuntimeWSClient 连 Runtime。
所有 CLI 参数见 `parse_args`。

启动：
    uv run python -m extensions.channels.monodesk --runtime-url=ws://127.0.0.1:8765 \
        --session-key=default
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from extensions.channels.monodesk import MonoDeskChannel, MonoDeskChannelConfig
from extensions.channels._runtime import run_channel
from core.runtime_ws_client import RuntimeWSClient

SOURCE = "monodesk"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MonoX monoDesk channel process")
    p.add_argument("--runtime-url", default="ws://127.0.0.1:8765")
    p.add_argument("--session-key", default="default")
    p.add_argument("--host", default="127.0.0.1", help="ws server host for desktop clients")
    p.add_argument("--port", type=int, default=8766)
    return p.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [monox:%(name)s] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cfg = MonoDeskChannelConfig(host=args.host, port=args.port, model="")
    channel = MonoDeskChannel(cfg, session_key=args.session_key)
    ws_client = RuntimeWSClient(
        url=args.runtime_url,
        hello_session_key=args.session_key,
        hello_source=SOURCE,
    )
    await run_channel(channel, ws_client=ws_client)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())