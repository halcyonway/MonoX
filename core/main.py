"""MonoX runtime 入口：装配所有组件。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from core.channel import TerminalChannel
from core.config import Config, session_paths
from core.gateway import Gateway
from core.llm_proxy import OpenAIStreamProxy
from core.loop import BashTool, ReadToolResultBudgetTool, SkillLoadTool, ToolRegistry
from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.engine import LoopEngine
from core.loop.skill_summary import SkillSummaryLoader
from core.memory import FsMemoryStore
from core.protocol import InboundEvent, StreamEvent
from core.sandbox import BashRunner


DEFAULT_SYSTEM = """You are MonoX, a coding agent. You run inside a sandboxed bash environment.

Plan briefly, then execute. Use bash for all I/O. Use skill_load to fetch details of a skill before invoking it.

Tool results may be L1-compressed; if you see budget_id, call read_tool_result_budget(budget_id=...) for the full version."""


def build_channel(cfg: Config):
    if cfg.channel.kind == "terminal":
        return TerminalChannel(cfg.session_key)
    raise NotImplementedError(f"channel kind not implemented: {cfg.channel.kind}")


async def run(cfg_path: str) -> None:
    cfg = Config.load(cfg_path)
    paths = session_paths(cfg)
    paths["workspace"].mkdir(parents=True, exist_ok=True)

    runner = BashRunner()
    budget_tool = ReadToolResultBudgetTool()
    tools = ToolRegistry(
        [
            BashTool(runner, paths["workspace"]),
            SkillLoadTool(Path(cfg.sandbox.skills_root)),
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

    channel = build_channel(cfg)
    input_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
    output_q: asyncio.Queue[StreamEvent] = asyncio.Queue()
    gateway = Gateway(channel, loop_input=input_q, loop_output=output_q)

    await asyncio.gather(gateway.run(), loop.run(input_q, output_q))


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "config.toml"))