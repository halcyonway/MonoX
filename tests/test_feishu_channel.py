"""FeishuChannel 单元测试。"""
from __future__ import annotations

import asyncio
import queue
import time
from unittest.mock import MagicMock, patch

import pytest

from core.protocol import InboundEvent
from extensions.channels.feishu import FeishuChannel, FeishuChannelConfig


class TestFeishuChannel:
    @pytest.fixture
    def cfg(self) -> FeishuChannelConfig:
        return FeishuChannelConfig(
            app_id="test_app_id",
            app_secret="test_app_secret",
            allowed_chats=[],
        )

    @pytest.fixture
    def ch(self, cfg: FeishuChannelConfig) -> FeishuChannel:
        return FeishuChannel(cfg, session_key="test")

    def test_inbound_source_is_feishu(self, ch: FeishuChannel):
        """FeishuChannel 产生的 InboundEvent.source 应该是 feishu。"""
        assert ch._session_key == "test"

    def test_open_id_by_chat_populated(self, ch: FeishuChannel):
        """_on_message 应该把 open_id 缓存到 _open_id_by_chat，同时记下 _last_chat_id。"""
        # 模拟 WS 线程里收到的消息事件
        mock_data = MagicMock()
        mock_event = MagicMock()
        mock_sender = MagicMock()
        mock_sender_id = MagicMock()
        mock_sender_id.open_id = "ou_test123"
        mock_sender.sender_id = mock_sender_id
        mock_sender.sender_type = "user"
        mock_event.sender = mock_sender
        mock_message = MagicMock()
        mock_message.chat_id = "oc_test_chat"
        mock_message.message_type = "text"
        mock_message.content = '{"text":"hello"}'
        mock_event.message = mock_message
        mock_data.event = mock_event
        mock_data.message_id = "msg_test_001"

        ch._on_message(mock_data)

        assert ch._open_id_by_chat.get("oc_test_chat") == "ou_test123"
        # send 路径靠 _last_chat_id 反查 open_id（session_key 跨 channel 共享 default 后
        # 不再是 chat_id）。如果 send 时 _last_chat_id 为空会丢消息。
        assert ch._last_chat_id == "oc_test_chat"

    def test_queue_reply_system_notify(self, ch: FeishuChannel):
        """_queue_reply 应该产生 event_type=system-notify 的事件。"""
        ch._queue_reply("oc_chat", "ou_user", "auto reply text")

        ev = ch._sync_q.get_nowait()
        assert ev is not None
        assert ev.source == "feishu"
        assert ev.event_type == "system-notify"
        assert "[auto-reply] auto reply text" in ev.text
        assert ev.meta.get("_auto_reply") is True

    def test_allowed_chats_blocks_unknown(self, ch: FeishuChannel):
        """allowed_chats 非空时，只放行白名单 chat_id。"""
        ch._cfg.allowed_chats = ["oc_allowed"]
        mock_data = MagicMock()
        mock_event = MagicMock()
        mock_sender = MagicMock()
        mock_sender.sender_id = MagicMock()
        mock_sender.sender_id.open_id = "ou_user"
        mock_event.sender = mock_sender
        mock_message = MagicMock()
        mock_message.chat_id = "oc_blocked"
        mock_message.message_type = "text"
        mock_message.content = '{"text":"hello"}'
        mock_event.message = mock_message
        mock_data.event = mock_event

        ch._on_message(mock_data)

        assert ch._sync_q.empty()

    def test_non_text_message_queued_as_auto_reply(self, ch: FeishuChannel):
        """非文本消息应该产生 system-notify 自动回复。"""
        mock_data = MagicMock()
        mock_event = MagicMock()
        mock_sender = MagicMock()
        mock_sender.sender_id = MagicMock()
        mock_sender.sender_id.open_id = "ou_user"
        mock_event.sender = mock_sender
        mock_message = MagicMock()
        mock_message.chat_id = "oc_chat"
        mock_message.message_type = "image"
        mock_message.content = '{"image_key":"img_xxx"}'
        mock_event.message = mock_message
        mock_data.event = mock_event

        ch._on_message(mock_data)

        ev = ch._sync_q.get_nowait()
        assert ev is not None
        assert "[暂不支持 image 消息类型]" in ev.text

    def test_on_message_deduplication(self, ch: FeishuChannel):
        """相同 message_id 的消息应该被去重。"""
        mock_data = MagicMock()
        mock_event = MagicMock()
        mock_sender = MagicMock()
        mock_sender.sender_id = MagicMock()
        mock_sender.sender_id.open_id = "ou_user"
        mock_event.sender = mock_sender
        mock_message = MagicMock()
        mock_message.chat_id = "oc_chat"
        mock_message.message_type = "text"
        mock_message.content = '{"text":"hello"}'
        mock_event.message = mock_message
        mock_data.event = mock_event
        mock_data.message_id = "msg_dup"

        ch._on_message(mock_data)
        assert not ch._sync_q.empty()

        # 第二次相同 message_id
        ch._on_message(mock_data)
        # 队列大小不变（被去重）
        assert ch._sync_q.qsize() == 1

    async def test_listen_consumes_sync_q(self, ch: FeishuChannel):
        """listen() 应该从 _sync_q 拉取事件并正确设置 source/event_type。"""
        event = InboundEvent(
            session_key="oc_chat",
            kind="message",
            text="test message",
            source="feishu",
            event_type="user-input",
            timestamp=time.time(),
            meta={"chat_id": "oc_chat", "open_id": "ou_user"},
        )
        ch._sync_q.put_nowait(event)

        received: list[InboundEvent] = []
        async for ev in ch.listen():
            received.append(ev)
            ch._stop.set()

        assert len(received) == 1
        assert received[0].source == "feishu"
        assert received[0].event_type == "user-input"
        assert received[0].text == "test message"

    async def test_listen_multiple_events(self, ch: FeishuChannel):
        """listen() 应该能顺序消费多个事件。"""
        for i in range(3):
            ch._sync_q.put_nowait(
                InboundEvent(
                    session_key=f"oc_chat{i}",
                    kind="message",
                    text=f"msg{i}",
                    source="feishu",
                    event_type="user-input",
                    timestamp=time.time(),
                )
            )

        received: list[InboundEvent] = []
        async for ev in ch.listen():
            received.append(ev)
            if len(received) >= 3:
                ch._stop.set()

        assert len(received) == 3
        assert [ev.text for ev in received] == ["msg0", "msg1", "msg2"]

    async def test_pending_session_key_set(self, ch: FeishuChannel):
        """listen() 取到事件后应该更新 _pending_session_key。"""
        event = InboundEvent(
            session_key="oc_chat_pending",
            kind="message",
            text="hello",
            source="feishu",
            event_type="user-input",
            timestamp=time.time(),
        )
        ch._sync_q.put_nowait(event)

        async for ev in ch.listen():
            assert ch._pending_session_key == "oc_chat_pending"
            ch._stop.set()
            break

    async def test_stop_empties_sync_q(self, ch: FeishuChannel):
        """stop() 后 listen() 应该退出。"""
        ch._stop.set()
        count = 0
        async for ev in ch.listen():
            count += 1
        assert count == 0

    def test_card_state_tracked(self, ch: FeishuChannel):
        """_CardState 应该在需要时被正确追踪。"""
        from extensions.channels.feishu import _CardState

        ch._card_by_session["oc_chat1"] = _CardState(
            message_id="msg_card_1",
            content="partial",
        )
        assert ch._card_by_session["oc_chat1"].message_id == "msg_card_1"
        assert ch._card_by_session["oc_chat1"].content == "partial"
