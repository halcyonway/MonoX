"""feishu channel 进程入口。

启动 FeishuChannel（lark-oapi WS + HTTP）+ RuntimeWSClient 连 Runtime。

feishu 场景下，`--session-key` 是默认值（单个 chat 入口）；实际每个 chat 会用
chat_id 作为 session_key，由 FeishuChannel 内部在 InboundEvent 上设置。

启动：
    uv run python -m extensions.channels.feishu --runtime-url=ws://127.0.0.1:8765 \
        --app-id=... --app-secret=... --allowed-chats=chat_id1,chat_id2
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from extensions.channels.feishu import FeishuChannel, FeishuChannelConfig
from extensions.channels._runtime import run_channel
from core.runtime_ws_client import RuntimeWSClient

SOURCE = "feishu"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MonoX feishu channel process")
    p.add_argument("--runtime-url", default="ws://127.0.0.1:8765")
    p.add_argument("--session-key", default="default",
                   help="Default session_key for ch_id-less messages; usually chat_id is used per inbound")
    p.add_argument("--app-id", default="")
    p.add_argument("--app-secret", default="")
    p.add_argument("--allowed-chats", default="",
                   help="Comma-separated chat_id whitelist; empty = allow all")
    return p.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [monox:%(name)s] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    allowed = [c.strip() for c in args.allowed_chats.split(",") if c.strip()]
    cfg = FeishuChannelConfig(
        app_id=args.app_id,
        app_secret=args.app_secret,
        allowed_chats=allowed,
    )
    channel = FeishuChannel(cfg, session_key=args.session_key)
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