"""MonoX 启动入口（装配所有组件）。

core/ 是稳定内核；装配在顶层 run.py。

    uv run python run.py [config.toml] [--debug] [--session_key NAME]
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
from pathlib import Path

from core.channel.base import Channel
from core.config import Config, session_paths
from core.gateway import Gateway, MultiChannelGateway
from core.llm_proxy import OpenAIStreamProxy
from core.loop import (
    BashTool,
    ReadToolResultBudgetTool,
    SkillLoadTool,
    ToolRegistry,
    WaitIoTool,
)
from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.compression import CompressionService
from core.loop.engine import LoopEngine
from core.loop.skill_summary import SkillSummaryLoader
from core.memory import FsMemoryStore
from core.protocol import InboundEvent, StreamEvent
from core.sandbox import BashRunner
from extensions.channels import (
    FeishuChannel,
    FeishuChannelConfig,
    MonoDeskChannel,
    MonoDeskChannelConfig,
    TerminalChannel,
    TextualChannel,
)


DEFAULT_SYSTEM = """You are MonoX, a coding agent. You run inside a sandboxed bash environment.

Plan briefly, then execute. Use bash for all I/O. Use skill_load to fetch details of a skill before invoking it.

Tool results may be L1-compressed; if you see budget_id, call read_tool_result_budget(budget_id=...) for the full version.

When you are done with the current turn and ready to receive the next message, call wait_io. If the user sends a new message while you are mid-task, it will be appended to the conversation and you can keep going."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MonoX agent runtime")
    parser.add_argument("config", nargs="?", default="config.toml", help="config.toml path")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show loop internals (state transitions, step metrics). Reasoning is always shown.",
    )
    parser.add_argument(
        "--session_key",
        default=None,
        help="Isolate workspace/memory/checkpoint under this name (default: 'default').",
    )
    return parser.parse_args()


def build_channel(cfg: Config, debug: bool) -> Channel:
    """单 channel 构建（兼容旧接口）。"""
    if cfg.channel.kind == "terminal":
        return TerminalChannel(cfg.session_key, debug=debug)
    if cfg.channel.kind == "textual":
        return TextualChannel(
            session_key=cfg.session_key,
            debug=debug,
            options=cfg.channel.options,
        )
    if cfg.channel.kind == "feishu":
        feishu_cfg = cfg.channel.channel_raw.get("feishu", {})
        return FeishuChannel(
            FeishuChannelConfig(
                app_id=feishu_cfg.get("app_id", ""),
                app_secret=feishu_cfg.get("app_secret", ""),
                allowed_chats=feishu_cfg.get("allowed_chats", []),
            ),
            session_key=cfg.session_key,
        )
    if cfg.channel.kind == "monodesk":
        return MonoDeskChannel(
            MonoDeskChannelConfig(
                host=cfg.channel.channel_raw.get("host", "127.0.0.1"),
                port=cfg.channel.channel_raw.get("port", 8765),
                model=cfg.channel.channel_raw.get("model", ""),
            ),
            session_key=cfg.session_key,
        )
    raise NotImplementedError(f"channel kind not implemented: {cfg.channel.kind}")


def _build_one_channel(
    kind: str,
    channel_raw: dict,
    session_key: str,
    debug: bool,
) -> Channel:
    """根据 kind 构建单个 channel。"""
    if kind == "terminal":
        return TerminalChannel(session_key=session_key, debug=debug)
    if kind == "textual":
        return TextualChannel(
            session_key=session_key,
            debug=debug,
            options=channel_raw.get("options", {}),
        )
    if kind == "feishu":
        return FeishuChannel(
            FeishuChannelConfig(
                app_id=channel_raw.get("app_id", ""),
                app_secret=channel_raw.get("app_secret", ""),
                allowed_chats=channel_raw.get("allowed_chats", []),
            ),
            session_key=session_key,
        )
    if kind == "monodesk":
        return MonoDeskChannel(
            MonoDeskChannelConfig(
                host=channel_raw.get("host", "127.0.0.1"),
                port=channel_raw.get("port", 8765),
                model=channel_raw.get("model", ""),
            ),
            session_key=session_key,
        )
    raise NotImplementedError(f"channel kind not implemented: {kind}")


def build_channels(cfg: Config, debug: bool) -> list[tuple[str, Channel]]:
    """多 channel 构建，返回 [(name, Channel), ...]。
    优先用 [[channels]] 新格式，fallback 到单 [channel] 旧格式。
    """
    if cfg.multi_channel.channels:
        return [
            (c.kind, _build_one_channel(c.kind, c.channel_raw, cfg.session_key, debug))
            for c in cfg.multi_channel.channels
        ]
    # 兼容旧格式：单 channel
    ch = build_channel(cfg, debug)
    return [(cfg.channel.kind, ch)]


def _ensure_dirs(paths: dict[str, Path]) -> None:
    """启动时确保所有 sandbox 目录存在。"""
    for key in ("workspace", "memory", "memory_notes", "tmp_root", "skills_root"):
        paths[key].mkdir(parents=True, exist_ok=True)


async def run(cfg_path: str, debug: bool, session_key: str | None) -> None:
    cfg = Config.load(cfg_path)
    if session_key:
        # CLI 覆盖 config.toml 里的 session_key
        cfg = dataclasses.replace(cfg, session_key=session_key)
    paths = session_paths(cfg)
    _ensure_dirs(paths)

    runner = BashRunner()
    budget_tool = ReadToolResultBudgetTool()
    tools = ToolRegistry(
        [
            BashTool(runner, paths["workspace"]),
            SkillLoadTool(Path(cfg.sandbox.skills_root)),
            WaitIoTool(),
            budget_tool,
        ]
    )

    memory = FsMemoryStore(Path(cfg.sandbox.memory_root))
    checkpoint = JsonlCheckpointStore(paths["checkpoint"])
    skill_summary = SkillSummaryLoader(Path(cfg.sandbox.skills_root)).summary()
    if cfg.compression_llm is None:
        raise RuntimeError(
            "missing [llm.compression]: a compression model is required; "
            "configure it in config.toml"
        )

    llm = OpenAIStreamProxy(cfg.llm)
    compression_llm = OpenAIStreamProxy(cfg.compression_llm)

    compression = CompressionService(
        budget_tool=budget_tool,
        llm=compression_llm,
        memory=memory,
    )

    loop = LoopEngine(
        session_key=cfg.session_key,
        system_prompt=DEFAULT_SYSTEM,
        llm=llm,
        tools=tools,
        compression=compression,
        memory=memory,
        checkpoint=checkpoint,
        skill_summary=skill_summary,
    )

    channels = build_channels(cfg, debug)
    input_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
    output_q: asyncio.Queue[StreamEvent] = asyncio.Queue()

    if len(channels) == 1:
        # 单 channel：用轻量 Gateway
        name, ch = channels[0]
        gateway: Gateway | MultiChannelGateway = Gateway(ch, loop_input=input_q, loop_output=output_q)
    else:
        # 多 channel：用 MultiChannelGateway
        default_ch = cfg.multi_channel.default_channel or "terminal"
        gateway = MultiChannelGateway(
            channels,
            loop_input=input_q,
            loop_output=output_q,
            default_channel=default_ch,
        )

    await asyncio.gather(gateway.run(), loop.run(input_q, output_q))


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(args.config, args.debug, args.session_key))
