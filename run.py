"""MonoX 启动入口（装配所有组件）。

core/ 是稳定内核；装配在顶层 run.py。

    uv run python run.py [config.toml] [--debug]

环境变量:
    MONOX_DEBUG=1   等价于 --debug
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from core.channel.base import Channel
from core.config import Config, session_paths
from core.gateway import Gateway
from core.llm_proxy import OpenAIStreamProxy
from core.loop import (
    BashTool,
    ReadToolResultBudgetTool,
    SkillLoadTool,
    ToolRegistry,
    WaitIoTool,
)
from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.engine import LoopEngine
from core.loop.skill_summary import SkillSummaryLoader
from core.memory import FsMemoryStore
from core.protocol import InboundEvent, StreamEvent
from core.sandbox import BashRunner
from extensions.channels import TerminalChannel


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
        help="Show loop internals (status, reasoning, metrics). Equiv to MONOX_DEBUG=1.",
    )
    return parser.parse_args()


def build_channel(cfg: Config, debug: bool) -> Channel:
    if cfg.channel.kind == "terminal":
        return TerminalChannel(cfg.session_key, debug=debug)
    raise NotImplementedError(f"channel kind not implemented: {cfg.channel.kind}")


def _ensure_dirs(paths: dict[str, Path]) -> None:
    """启动时确保所有 sandbox 目录存在。"""
    for key in ("workspace", "memory", "memory_notes", "tmp_root", "skills_root"):
        paths[key].mkdir(parents=True, exist_ok=True)


async def run(cfg_path: str, debug: bool) -> None:
    cfg = Config.load(cfg_path)
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
    llm = OpenAIStreamProxy(cfg.llm)

    loop = LoopEngine(
        session_key=cfg.session_key,
        system_prompt=DEFAULT_SYSTEM,
        llm=llm,
        tools=tools,
        budget_tool=budget_tool,
        memory=memory,
        checkpoint=checkpoint,
        skill_summary=skill_summary,
    )

    channel = build_channel(cfg, debug)
    input_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
    output_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
    gateway = Gateway(channel, loop_input=input_q, loop_output=output_q)

    await asyncio.gather(gateway.run(), loop.run(input_q, output_q))


if __name__ == "__main__":
    args = parse_args()
    debug = args.debug or os.environ.get("MONOX_DEBUG") == "1"
    asyncio.run(run(args.config, debug))