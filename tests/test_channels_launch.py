"""channel `__main__.py` smoke 测试。

不真正起 ws server / desktop / feishu，只验证：
- 每个 channel 的 __main__ 可 import
- parse_args([]) 返回合理默认
- _runtime.run_channel 可被构造调用（不连接真 ws，用 mock）

更深入的集成测试由手测覆盖（每个 channel 是独立进程）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from extensions.channels import _runtime
from extensions.channels.feishu import __main__ as feishu_main
from extensions.channels.terminal import __main__ as terminal_main
from extensions.channels.textual_chat import __main__ as textual_main


def test_all_channel_modules_importable():
    """3 个 channel 的 __main__ 模块都能 import 不报错。"""
    assert terminal_main is not None
    assert feishu_main is not None
    assert textual_main is not None


def test_terminal_default_args():
    args = terminal_main.parse_args([])
    assert args.runtime_url == "ws://127.0.0.1:8765"
    assert args.session_key == "default"


def test_feishu_default_args():
    args = feishu_main.parse_args([])
    assert args.runtime_url == "ws://127.0.0.1:8765"
    assert args.session_key == "default"


def test_textual_default_args():
    args = textual_main.parse_args([])
    assert args.runtime_url == "ws://127.0.0.1:8765"
    assert args.session_key == "default"


def test_runtime_helper_pumps_with_mocks():
    """_runtime.run_channel 用 mock channel + ws_client 验证 pump 路径可启动。"""
    import asyncio

    async def _go():
        channel = MagicMock()
        channel.start = MagicMock(return_value=asyncio.sleep(0))
        channel.listen = MagicMock(return_value=_empty_async_iter())
        channel.stop = MagicMock(return_value=asyncio.sleep(0))
        ws_client = MagicMock()
        ws_client.run = MagicMock(return_value=asyncio.sleep(0.05))  # 跑 50ms 后停
        ws_client.stop = MagicMock(return_value=asyncio.sleep(0))
        # pump_outbound 会 await ws_client.recv() → 给个 AsyncMock
        async def _never_recv():
            await asyncio.Event().wait()  # hang until cancelled
        ws_client.recv = _never_recv

        await _runtime.run_channel(channel, ws_client=ws_client)

    async def _empty_async_iter():
        if False:
            yield  # make it an async generator

    try:
        asyncio.run(asyncio.wait_for(_go(), timeout=2))
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass  # 我们只是验证能进入 run_channel；pumps 会 hang，timeout 即可


def test_supervisor_restarts_channel_after_crash():
    """_run_once 抛 OSError → supervisor 退避重启 → 下次 _run_once 应被再调用。"""
    import asyncio

    async def _go():
        call_count = {"n": 0}

        def _make_channel():
            ch = MagicMock()
            ch.start = MagicMock(side_effect=lambda: _raise_then_succeed(call_count))
            ch.stop = MagicMock(return_value=asyncio.sleep(0))

            async def _empty_iter():
                if False: yield
            ch.listen = MagicMock(return_value=_empty_iter())
            return ch

        async def _raise_then_succeed(cc):
            cc["n"] += 1
            if cc["n"] == 1:
                raise OSError(48, "Address already in use")
            await asyncio.Event().wait()  # 成功后挂住

        ch = _make_channel()
        ws = MagicMock()
        ws.run = MagicMock(return_value=asyncio.sleep(0))
        ws.stop = MagicMock(return_value=asyncio.sleep(0))
        ws.recv = MagicMock(side_effect=lambda: _never())

        async def _never():
            await asyncio.Event().wait()

        # 短超时——只要 supervisor 进入第二次 attempt 就退出
        try:
            await asyncio.wait_for(_runtime.run_channel(ch, ws_client=ws), timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        # 验证 supervisor 确实重启了
        assert call_count["n"] >= 2, f"supervisor should restart channel after OSError; got {call_count['n']} calls"

    asyncio.run(_go())
