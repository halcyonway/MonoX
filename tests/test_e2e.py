"""端到端 pipeline 回归测试（Mock LLM）。

覆盖：
- Channel → Gateway → Loop → Tool 完整链路
- Mock LLM 两次调用（tool_call + final）
- BashTool 真实执行（echo hello）
- Checkpoint 持久化
- StreamEvent 类型与时序
- wait_io tool：agent 主动结束当前 turn
- queue aggregate：react 中追加新 user message 继续 react
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
from core.loop.tools import BashTool, ReadToolResultBudgetTool, SkillLoadTool, WaitIoTool
from core.memory import FsMemoryStore
from core.protocol import (
    FinalMessage,
    InboundEvent,
    LlmChunk,
    LLMProxy,
    ReasoningChunk,
    StatusChange,
    StreamEvent,
)
from core.sandbox import BashRunner


class MockLLM(LLMProxy):
    def __init__(self, scripts: list[list[LlmChunk]], step_delay: float = 0.0) -> None:
        self._scripts = scripts
        self._idx = 0
        self._step_delay = step_delay

    async def stream(self, messages, tools=None, options=None):
        if self._idx >= len(self._scripts):
            yield LlmChunk(delta_text="[end]", finish_reason="stop")
            return
        if self._step_delay:
            await asyncio.sleep(self._step_delay)
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


def build(tmp: Path) -> tuple[ToolRegistry, FsMemoryStore, JsonlCheckpointStore, str]:
    ws = tmp / "ws"; ws.mkdir(parents=True, exist_ok=True)
    mem = tmp / "mem"; mem.mkdir(exist_ok=True)
    skills = tmp / "skills"; skills.mkdir(exist_ok=True)

    runner = BashRunner()
    budget = ReadToolResultBudgetTool()
    tools = ToolRegistry([
        BashTool(runner, ws), SkillLoadTool(skills), WaitIoTool(), budget,
    ])
    mem_store = FsMemoryStore(mem)
    ck = JsonlCheckpointStore(mem / "default" / "checkpoint.jsonl")
    skill_sum = SkillSummaryLoader(skills).summary()
    return tools, mem_store, ck, skill_sum


async def run_pipeline(tmp: Path, llm: MockLLM, events: list[InboundEvent]) -> MockChannel:
    tools, mem_store, ck, skill_sum = build(tmp)
    loop = LoopEngine(
        session_key="default", system_prompt="test",
        llm=llm, tools=tools, budget_tool=tools.get("read_tool_result_budget"),
        memory=mem_store, checkpoint=ck, skill_summary=skill_sum,
    )
    ch = MockChannel(events)
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
    return ch


async def test_basic_bash() -> None:
    """Mock LLM 调 bash tool，最后 final message。"""
    tmp = Path("/tmp/test_e2e_basic"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
    llm = MockLLM([
        [
            LlmChunk(delta_text="Run it. "),
            LlmChunk(delta_tool_calls=(
                {"index": 0, "id": "c1", "function": {"name": "bash", "arguments": '{"cmd":"echo hello"}'}},
            )),
            LlmChunk(finish_reason="tool_calls"),
        ],
        [LlmChunk(delta_text="All done."), LlmChunk(finish_reason="stop")],
    ])
    ch = await run_pipeline(tmp, llm, [InboundEvent(session_key="default", kind="message", text="say hi")])
    final = next(e for e in ch._sent if isinstance(e, FinalMessage))
    assert "All done" in final.text
    tool_ends = [e for e in ch._sent if type(e).__name__ == "ToolEnd"]
    assert len(tool_ends) == 1
    assert tool_ends[0].result.stdout.strip() == "hello"
    assert llm._idx == 2
    print("test_basic_bash PASSED ✓")


async def test_wait_io_ends_turn() -> None:
    """agent 主动调 wait_io → react 立即结束，不调第二次 LLM。"""
    tmp = Path("/tmp/test_e2e_waitio"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
    llm = MockLLM([
        [
            LlmChunk(delta_text="Pausing. "),
            LlmChunk(delta_tool_calls=(
                {"index": 0, "id": "c1", "function": {"name": "wait_io", "arguments": '{"reason":"need input"}'}},
            )),
            LlmChunk(finish_reason="tool_calls"),
        ],
    ])
    ch = await run_pipeline(tmp, llm, [InboundEvent(session_key="default", kind="message", text="ask")])
    # wait_io tool 应被 dispatch
    tool_starts = [e for e in ch._sent if type(e).__name__ == "ToolStart"]
    assert any(s.name == "wait_io" for s in tool_starts)
    # StatusChange(wait_io) 应出现
    wait_io_states = [e for e in ch._sent if isinstance(e, StatusChange) and e.state == "wait_io"]
    assert len(wait_io_states) == 1
    # LLM 只调一次
    assert llm._idx == 1
    # FinalMessage 触发
    assert any(isinstance(e, FinalMessage) for e in ch._sent)
    print("test_wait_io_ends_turn PASSED ✓")


async def test_queue_aggregate_continues_react() -> None:
    """agent 完成 final 时 input_queue 有新事件 → aggregate 继续 react。"""
    tmp = Path("/tmp/test_e2e_agg"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()

    llm = MockLLM([
        [LlmChunk(delta_text="first reply"), LlmChunk(finish_reason="stop")],
        [LlmChunk(delta_text="second reply"), LlmChunk(finish_reason="stop")],
    ], step_delay=0.3)

    tools, mem_store, ck, skill_sum = build(tmp)
    loop = LoopEngine(
        session_key="default", system_prompt="test",
        llm=llm, tools=tools, budget_tool=tools.get("read_tool_result_budget"),
        memory=mem_store, checkpoint=ck, skill_summary=skill_sum,
    )
    ch = MockChannel([InboundEvent(session_key="default", kind="message", text="first")])
    iq: asyncio.Queue[InboundEvent] = asyncio.Queue()
    oq: asyncio.Queue[StreamEvent] = asyncio.Queue()
    gw = Gateway(ch, loop_input=iq, loop_output=oq)
    gw_task = asyncio.create_task(gw.run())
    loop_task = asyncio.create_task(loop.run(iq, oq))

    # 等第一次 LLM 调用完成（drain 检测到 second 必须在 step 1 final 后）
    await asyncio.sleep(0.2)
    iq.put_nowait(InboundEvent(session_key="default", kind="message", text="second"))

    await asyncio.wait_for(ch._final_done.wait(), timeout=10.0)
    await asyncio.sleep(0.2)
    gw_task.cancel(); loop_task.cancel()
    for t in (gw_task, loop_task):
        try: await t
        except: pass

    assert llm._idx == 2
    final = next(e for e in ch._sent if isinstance(e, FinalMessage))
    assert "second reply" in final.text
    print("test_queue_aggregate_continues_react PASSED ✓")


async def test_chat_only_persists_across_restart() -> None:
    """纯对话 turn（无 tool）也必须写 checkpoint；重启后 messages 完整恢复。
    模拟用户场景：run.py 启 → 对话 → Ctrl+C → 再启，agent 仍记得上次聊了什么。
    """
    tmp = Path("/tmp/test_e2e_persist"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()

    # turn 1：纯对话，agent 说 "first reply" 就停
    llm1 = MockLLM([
        [LlmChunk(delta_text="first reply", finish_reason="stop")],
    ])
    ch1 = await run_pipeline(tmp, llm1, [
        InboundEvent(session_key="default", kind="message", text="hi there")
    ])
    final1 = next(e for e in ch1._sent if isinstance(e, FinalMessage))
    assert "first reply" in final1.text

    # checkpoint 文件必须存在且非空
    ck_path = tmp / "mem" / "default" / "checkpoint.jsonl"
    assert ck_path.exists(), "checkpoint.jsonl should exist after pure chat turn"
    assert ck_path.stat().st_size > 0, "checkpoint.jsonl should be non-empty"

    # turn 2：重启，新 LLM 看上一次 user msg 是否在 messages 里
    captured_messages: list[list[dict]] = []

    class CapturingLLM(LLMProxy):
        async def stream(self, messages, tools=None, options=None):
            captured_messages.append(list(messages))
            yield LlmChunk(delta_text="remembered", finish_reason="stop")

    tools, mem_store, ck, skill_sum = build(tmp)
    loop = LoopEngine(
        session_key="default", system_prompt="test",
        llm=CapturingLLM(), tools=tools, budget_tool=tools.get("read_tool_result_budget"),
        memory=mem_store, checkpoint=ck, skill_summary=skill_sum,
    )
    ch2 = MockChannel([InboundEvent(session_key="default", kind="message", text="again")])
    iq: asyncio.Queue[InboundEvent] = asyncio.Queue()
    oq: asyncio.Queue[StreamEvent] = asyncio.Queue()
    gw = Gateway(ch2, loop_input=iq, loop_output=oq)
    gw_task = asyncio.create_task(gw.run())
    loop_task = asyncio.create_task(loop.run(iq, oq))
    await asyncio.wait_for(ch2._final_done.wait(), timeout=10.0)
    await asyncio.sleep(0.2)
    gw_task.cancel(); loop_task.cancel()
    for t in (gw_task, loop_task):
        try: await t
        except: pass

    # 第一次 LLM 调用时 messages 必须包含上次的 user message "hi there"
    assert len(captured_messages) >= 1
    first_call_msgs = captured_messages[0]
    user_texts = [m["content"] for m in first_call_msgs if m["role"] == "user"]
    assert "hi there" in user_texts, (
        f"previous user message missing after restart; got {user_texts!r}"
    )
    print("test_chat_only_persists_across_restart PASSED ✓")


async def main() -> None:
    await test_basic_bash()
    await test_wait_io_ends_turn()
    await test_queue_aggregate_continues_react()
    await test_chat_only_persists_across_restart()
    print("\nALL TESTS PASSED ✓")


if __name__ == "__main__":
    asyncio.run(main())