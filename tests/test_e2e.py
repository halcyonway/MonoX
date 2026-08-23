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
import json
import shutil
from pathlib import Path

from core.channel.base import Channel
from core.llm_proxy import OpenAIStreamProxy  # noqa: F401  验证 import
from tests._inprocess_bridge import InProcessBridge
from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.compression import CompressionService
from core.loop.engine import LoopEngine
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
from core.skill_service import SkillService


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


def build(tmp: Path) -> tuple[ToolRegistry, FsMemoryStore, JsonlCheckpointStore, SkillService]:
    ws = tmp / "ws"; ws.mkdir(parents=True, exist_ok=True)
    mem = tmp / "mem"; mem.mkdir(exist_ok=True)
    skills = tmp / "skills"; skills.mkdir(exist_ok=True)

    runner = BashRunner()
    budget = ReadToolResultBudgetTool()
    skill_service = SkillService(skills)
    tools = ToolRegistry([
        BashTool(runner, ws), SkillLoadTool(skill_service), WaitIoTool(), budget,
    ])
    mem_store = FsMemoryStore(mem)
    ck = JsonlCheckpointStore(mem / "default" / "checkpoint.jsonl")
    return tools, mem_store, ck, skill_service


def make_compression(tools: ToolRegistry, llm, mem_store: FsMemoryStore) -> CompressionService:
    return CompressionService(
        budget_tool=tools.get("read_tool_result_budget"),
        llm=llm,
    )


async def run_pipeline(tmp: Path, llm: MockLLM, events: list[InboundEvent]) -> MockChannel:
    tools, mem_store, ck, skill_service = build(tmp)
    loop = LoopEngine(
        session_key="default", system_prompt="test",
        llm=llm, tools=tools, compression=make_compression(tools, llm, mem_store),
        memory=mem_store, checkpoint=ck, skill_service=skill_service,
    )
    ch = MockChannel(events)
    iq: asyncio.Queue[InboundEvent] = asyncio.Queue()
    oq: asyncio.Queue[StreamEvent] = asyncio.Queue()
    gw = InProcessBridge(ch, loop_input=iq, loop_output=oq)
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

    tools, mem_store, ck, skill_service = build(tmp)
    loop = LoopEngine(
        session_key="default", system_prompt="test",
        llm=llm, tools=tools, compression=make_compression(tools, llm, mem_store),
        memory=mem_store, checkpoint=ck, skill_service=skill_service,
    )
    ch = MockChannel([InboundEvent(session_key="default", kind="message", text="first")])
    iq: asyncio.Queue[InboundEvent] = asyncio.Queue()
    oq: asyncio.Queue[StreamEvent] = asyncio.Queue()
    gw = InProcessBridge(ch, loop_input=iq, loop_output=oq)
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

    tools, mem_store, ck, skill_service = build(tmp)
    capturing_llm = CapturingLLM()
    loop = LoopEngine(
        session_key="default", system_prompt="test",
        llm=capturing_llm, tools=tools, compression=make_compression(tools, capturing_llm, mem_store),
        memory=mem_store, checkpoint=ck, skill_service=skill_service,
    )
    ch2 = MockChannel([InboundEvent(session_key="default", kind="message", text="again")])
    iq: asyncio.Queue[InboundEvent] = asyncio.Queue()
    oq: asyncio.Queue[StreamEvent] = asyncio.Queue()
    gw = InProcessBridge(ch2, loop_input=iq, loop_output=oq)
    gw_task = asyncio.create_task(gw.run())
    loop_task = asyncio.create_task(loop.run(iq, oq))
    await asyncio.wait_for(ch2._final_done.wait(), timeout=10.0)
    await asyncio.sleep(0.2)
    gw_task.cancel(); loop_task.cancel()
    for t in (gw_task, loop_task):
        try: await t
        except: pass

    # 第一次 LLM 调用时 messages 必须包含上次的 user message "hi there"。
    # user message 现在用 XML event 包装（user_input kind），body 在 element 内。
    assert len(captured_messages) >= 1
    first_call_msgs = captured_messages[0]
    user_texts = [m["content"] for m in first_call_msgs if m["role"] == "user"]
    assert any("hi there" in c for c in user_texts), (
        f"previous user message missing after restart; got {user_texts!r}"
    )
    # 而且每条 user message 都是合法 XML event
    for c in user_texts:
        assert "<event" in c and ('kind="user_input"' in c)
        import xml.etree.ElementTree as ET
        ET.fromstring(c)  # well-formed XML
    print("test_chat_only_persists_across_restart PASSED ✓")


async def test_l1_tool_result_truncated() -> None:
    """bash 输出 5000 字符被 L1 截断，ToolEnd 带 budget_id。"""
    tmp = Path("/tmp/test_e2e_l1"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()

    cmd = "python3 -c \"print(chr(65)*5000)\""
    llm = MockLLM([
        [
            LlmChunk(delta_text="Generate. "),
            LlmChunk(delta_tool_calls=(
                {
                    "index": 0,
                    "id": "c1",
                    "function": {"name": "bash", "arguments": json.dumps({"cmd": cmd})},
                },
            )),
            LlmChunk(finish_reason="tool_calls"),
        ],
        [LlmChunk(delta_text="Done."), LlmChunk(finish_reason="stop")],
    ])

    ch = await run_pipeline(tmp, llm, [InboundEvent(session_key="default", kind="message", text="run")])
    tool_ends = [e for e in ch._sent if type(e).__name__ == "ToolEnd"]
    assert len(tool_ends) == 1
    result = tool_ends[0].result
    assert result.truncated is True
    assert result.budget_id is not None
    assert len(result.stdout) < 5000
    assert "read_tool_result_budget" in result.stdout
    print("test_l1_tool_result_truncated PASSED ✓")


async def test_l2_compression_folds_early_turns() -> None:
    """多个 user turn 超过阈值触发 L2 摘要并折叠最早轮。"""
    tmp = Path("/tmp/test_e2e_l2"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()

    big = "x" * 10_000
    events = [
        InboundEvent(session_key="default", kind="message", text="first turn " + big),
        InboundEvent(session_key="default", kind="message", text="second turn " + big),
        InboundEvent(session_key="default", kind="message", text="third turn " + big),
    ]

    class L2LLM(LLMProxy):
        def __init__(self) -> None:
            self.summary_calls = 0

        async def stream(self, messages, tools=None, options=None):
            if tools is None:
                self.summary_calls += 1
                yield LlmChunk(delta_text="folded summary", finish_reason="stop")
            else:
                yield LlmChunk(delta_text="final answer", finish_reason="stop")

    llm = L2LLM()
    tools, mem_store, ck, skill_service = build(tmp)
    compression = CompressionService(
        budget_tool=tools.get("read_tool_result_budget"),
        llm=llm,
        l2_char_threshold=100,
    )
    loop = LoopEngine(
        session_key="default", system_prompt="test",
        llm=llm, tools=tools, compression=compression,
        memory=mem_store, checkpoint=ck, skill_service=skill_service,
    )
    ch = MockChannel([])
    iq: asyncio.Queue[InboundEvent] = asyncio.Queue()
    oq: asyncio.Queue[StreamEvent] = asyncio.Queue()
    for e in events:
        iq.put_nowait(e)

    gw = InProcessBridge(ch, loop_input=iq, loop_output=oq)
    gw_task = asyncio.create_task(gw.run())
    loop_task = asyncio.create_task(loop.run(iq, oq))
    await asyncio.wait_for(ch._final_done.wait(), timeout=10.0)
    await asyncio.sleep(0.2)
    gw_task.cancel(); loop_task.cancel()
    for t in (gw_task, loop_task):
        try: await t
        except: pass

    assert llm.summary_calls == 1
    final = next(e for e in ch._sent if isinstance(e, FinalMessage))
    assert "final answer" in final.text
    assert any(isinstance(e, StatusChange) and e.state == "compressing" for e in ch._sent)
    # L3 已删除：压缩摘要不再自动落 Memory.md。
    print("test_l2_compression_folds_early_turns PASSED ✓")


async def main() -> None:
    await test_basic_bash()
    await test_wait_io_ends_turn()
    await test_queue_aggregate_continues_react()
    await test_chat_only_persists_across_restart()
    await test_l1_tool_result_truncated()
    await test_l2_compression_folds_early_turns()
    print("\nALL TESTS PASSED ✓")


if __name__ == "__main__":
    asyncio.run(main())