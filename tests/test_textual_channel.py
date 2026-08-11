"""TextualChannel headless smoke test。

用 App.run_test() + Pilot 驱动真实 App（headless），验证：
- send(event) → widget 更新
- submit_user_input → in_q 投递
- exit / Ctrl+C → 干净退出

不依赖真实 TTY；适合 CI。

注：smoke test 不通过 TextualChannel.start()（因为 run_async 会接管 stdout/stderr，
跟 Pilot 的 run_test 上下文冲突）。而是直接构造 ChatApp + run_test()。
exit 流程单独测：start() 启 run_async → submit "exit" → _run_task 完成。
"""
from __future__ import annotations

import asyncio

from core.protocol import (
    FinalMessage,
    InboundEvent,
    ReasoningChunk,
    StatusChange,
    TokenChunk,
    ToolEnd,
    ToolStart,
    ToolResult,
)
from extensions.channels.textual_chat import (
    AssistantMessage,
    ChatApp,
    DebugPanel,
    ReasoningMessage,
    StatusBar,
    TextualChannel,
    ToolCard,
    UserMessage,
)


async def _new_app_run_test():
    """构造 channel + ChatApp，进入 run_test() 上下文。返回 (channel, app, pilot)。"""
    ch = TextualChannel(session_key="t", debug=False)
    app = ChatApp(channel=ch)
    ch._app = app
    pilot_cm = app.run_test()
    pilot = await pilot_cm.__aenter__()
    await pilot.pause()
    return ch, app, pilot, pilot_cm


async def test_send_token_streaming() -> None:
    """send(TokenChunk) 流式累积到 AssistantMessage；non-token event 触发 freeze。"""
    ch, app, pilot, pilot_cm = await _new_app_run_test()
    try:
        await ch.send(StatusChange(state="thinking"))
        for tok in ("hello ", "world", "!"):
            await ch.send(TokenChunk(text=tok))
        await ch.send(StatusChange(state="wait_io"))  # 触发 freeze
        await asyncio.sleep(0.3)
        await pilot.pause()
        history = app.query_one("#history")
        md_list = list(history.query(AssistantMessage))
        assert len(md_list) == 1, f"expected 1 AssistantMessage, got {len(md_list)}"
        assert "hello world!" in md_list[0].source, (
            f"streamed text mismatch: {md_list[0].source!r}"
        )
        print("test_send_token_streaming PASSED ✓")
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def test_send_reasoning_buffered() -> None:
    """ReasoningChunk 累积 → 非 reasoning event 时冻结。"""
    ch, app, pilot, pilot_cm = await _new_app_run_test()
    try:
        await ch.send(ReasoningChunk(text="thinking step 1 "))
        await ch.send(ReasoningChunk(text="step 2"))
        await ch.send(StatusChange(state="thinking"))  # flush reasoning
        await asyncio.sleep(0.2)
        await pilot.pause()
        history = app.query_one("#history")
        rs = list(history.query(ReasoningMessage))
        assert len(rs) == 1, f"expected 1 ReasoningMessage, got {len(rs)}"
        content = rs[0]._buf
        assert "thinking step 1" in content and "step 2" in content, (
            f"reasoning frozen content: {content!r}"
        )
        print("test_send_reasoning_buffered PASSED ✓")
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def test_tool_card_start_end() -> None:
    """ToolStart 创建 ToolCard，ToolEnd 回填 stdout + latency。"""
    ch, app, pilot, pilot_cm = await _new_app_run_test()
    try:
        await ch.send(ToolStart(name="bash", args={"cmd": "echo hi"}))
        await ch.send(ToolEnd(
            name="bash",
            result=ToolResult(
                call_id="c1", status="ok",
                stdout="hi\n", stderr="", exit_code=0,
            ),
            latency_ms=42,
        ))
        await asyncio.sleep(0.2)
        await pilot.pause()
        history = app.query_one("#history")
        cards = list(history.query(ToolCard))
        assert len(cards) == 1
        content = str(cards[0].render()) + cards[0]._header
        assert "bash" in content
        assert "hi" in content
        assert "42ms" in content
        print("test_tool_card_start_end PASSED ✓")
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def test_submit_user_input_yields_inbound() -> None:
    """submit_user_input → in_q 投递；listen() 异步生成器能产出。"""
    ch, app, pilot, pilot_cm = await _new_app_run_test()
    try:
        ch.submit_user_input("hello agent")
        ev = await asyncio.wait_for(ch._in_q.get(), timeout=2.0)
        assert isinstance(ev, InboundEvent)
        assert ev.kind == "message"
        assert ev.text == "hello agent"
        assert ev.session_key == "t"
        print("test_submit_user_input_yields_inbound PASSED ✓")
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def test_exit_via_run_async_shuts_down() -> None:
    """真实 run_async 路径：start() 后 submit 'exit' → _run_task 完成、_stop set。"""
    ch = TextualChannel(session_key="t", debug=False)
    await ch.start()
    await asyncio.sleep(0.5)  # wait for App to mount in run_async
    ch.submit_user_input("exit")
    await asyncio.wait_for(ch._run_task, timeout=3.0)
    assert ch._stop.is_set()
    print("test_exit_via_run_async_shuts_down PASSED ✓")


async def test_f12_toggles_debug_panel() -> None:
    """F12 切换 DebugPanel visible 类。"""
    ch, app, pilot, pilot_cm = await _new_app_run_test()
    try:
        panel = app.query_one(DebugPanel)
        assert "visible" not in panel.classes
        await pilot.press("f12")
        await pilot.pause()
        assert "visible" in panel.classes
        await pilot.press("f12")
        await pilot.pause()
        assert "visible" not in panel.classes
        print("test_f12_toggles_debug_panel PASSED ✓")
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def test_status_bar_reflects_state() -> None:
    """StatusChange 更新 status bar 的文本。"""
    ch, app, pilot, pilot_cm = await _new_app_run_test()
    try:
        sb = app.query_one(StatusBar)
        await ch.send(StatusChange(state="thinking"))
        await asyncio.sleep(0.1)
        await pilot.pause()
        content = sb.state_text
        assert "thinking" in content
        await ch.send(StatusChange(state="wait_io"))
        await asyncio.sleep(0.1)
        await pilot.pause()
        content = sb.state_text
        assert "wait_io" in content
        print("test_status_bar_reflects_state PASSED ✓")
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def main() -> None:
    await test_send_token_streaming()
    await test_send_reasoning_buffered()
    await test_tool_card_start_end()
    await test_submit_user_input_yields_inbound()
    await test_f12_toggles_debug_panel()
    await test_status_bar_reflects_state()
    await test_exit_via_run_async_shuts_down()
    print("\nALL TEXTUAL SMOKE TESTS PASSED ✓")


if __name__ == "__main__":
    asyncio.run(main())