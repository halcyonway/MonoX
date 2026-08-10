"""端到端 pipeline 回归测试（Mock LLM）。

覆盖：
- Channel → Gateway → Loop → Tool 完整链路
- Mock LLM 两次调用（tool_call + final）
- BashTool 真实执行（echo hello）
- Checkpoint 持久化
- StreamEvent 类型与时序
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from core.channel.base import Channel
from core.gateway import Gateway
from core.llm_proxy import OpenAIStreamProxy  # noqa: F401  验证 import
from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.engine import LoopEngine
from core.loop.skill_summary import SkillSummaryLoader
from core.loop.tool_registry import ToolRegistry
from core.loop.tools import BashTool, ReadToolResultBudgetTool, SkillLoadTool
from core.memory import FsMemoryStore
from core.protocol import (
    FinalMessage,
    InboundEvent,
    LlmChunk,
    LLMProxy,
    StreamEvent,
)
from core.sandbox import BashRunner


class MockLLM(LLMProxy):
    def __init__(self, scripts: list[list[LlmChunk]]) -> None:
        self._scripts = scripts
        self._idx = 0

    async def stream(self, messages, tools=None):
        if self._idx >= len(self._scripts):
            yield LlmChunk(delta_text="[end]", finish_reason="stop")
            return
        for chunk in self._scripts[self._idx]:
            yield chunk
        self._idx += 1


class MockChannel(Channel):
    def __init__(self, events: list[InboundEvent]) -> None:
        self._events = events
        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._sent: list[StreamEvent] = []
        self._final_done = asyncio.Event()

    async def start(self) -> None:
        for e in self._events:
            await self._queue.put(e)

    async def stop(self) -> None:
        self._stop.set()

    async def listen(self):
        while not self._stop.is_set():
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=0.3)
            except asyncio.TimeoutError:
                continue

    async def send(self, event: StreamEvent) -> None:
        self._sent.append(event)
        if isinstance(event, FinalMessage):
            self._final_done.set()


async def run_pipeline(tmp: Path) -> tuple[MockChannel, MockLLM, JsonlCheckpointStore]:
    ws = tmp / "ws"; ws.mkdir(parents=True)
    mem = tmp / "mem"; mem.mkdir()
    skills = tmp / "skills"; skills.mkdir()

    runner = BashRunner()
    budget = ReadToolResultBudgetTool()
    tools = ToolRegistry([
        BashTool(runner, ws), SkillLoadTool(skills), budget,
    ])
    mem_store = FsMemoryStore(mem)
    ck = JsonlCheckpointStore(mem / "default" / "checkpoint.jsonl")
    skill_sum = SkillSummaryLoader(skills).summary()

    llm = MockLLM([
        [
            LlmChunk(delta_text="Run it. "),
            LlmChunk(delta_tool_calls=(
                {"index": 0, "id": "c1", "function": {"name": "bash", "arguments": '{"cmd":"echo hello"}'}},
            )),
            LlmChunk(finish_reason="tool_calls"),
        ],
        [
            LlmChunk(delta_text="All done."),
            LlmChunk(finish_reason="stop"),
        ],
    ])

    loop = LoopEngine(
        session_key="default", system_prompt="test",
        llm=llm, tools=tools, budget_tool=budget,
        memory=mem_store, checkpoint=ck, skill_summary=skill_sum,
    )

    ch = MockChannel([InboundEvent(session_key="default", kind="message", text="say hi")])
    iq: asyncio.Queue[InboundEvent] = asyncio.Queue()
    oq: asyncio.Queue[StreamEvent] = asyncio.Queue()
    gw = Gateway(ch, loop_input=iq, loop_output=oq)

    gw_task = asyncio.create_task(gw.run())
    loop_task = asyncio.create_task(loop.run(iq, oq))

    await asyncio.wait_for(ch._final_done.wait(), timeout=10.0)
    await asyncio.sleep(0.2)

    gw_task.cancel(); loop_task.cancel()
    for t in (gw_task, loop_task):
        try: await t
        except: pass

    return ch, llm, ck


async def main() -> None:
    tmp = Path("/tmp/test_e2e")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()

    ch, llm, ck = await run_pipeline(tmp)

    final = next(e for e in ch._sent if isinstance(e, FinalMessage))
    assert "All done" in final.text

    tool_ends = [e for e in ch._sent if type(e).__name__ == "ToolEnd"]
    assert len(tool_ends) == 1
    assert tool_ends[0].result.stdout.strip() == "hello"

    assert llm._idx == 2

    ck_path = ck._path
    assert ck_path.exists()
    lines = [l for l in ck_path.read_text().strip().split("\n") if l]
    assert len(lines) == 1

    print("ALL ASSERTIONS PASSED ✓")


if __name__ == "__main__":
    asyncio.run(main())