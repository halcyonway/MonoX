"""Feishu IM channel adapter — WebSocket long connection via lark-oapi.

入站：飞书开放平台 → im.message.receive_v1 → InboundEvent
出站：用交互卡片（interactive card）流式更新内容：
      StatusChange(thinking) → 发空白卡片
      TokenChunk            → patch 卡片追加内容
      FinalMessage          → patch 为最终内容

ws.Client.start() 同步阻塞，在独立线程里跑，不阻塞 MonoX 的 asyncio 主循环。
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws import Client as FeishuWSClient
from lark_oapi.core.enum import LogLevel

from core.channel.base import Channel
from core.protocol import (
    ErrorEvent,
    FinalMessage,
    InboundEvent,
    StatusChange,
    StreamEvent,
    TokenChunk,
)


@dataclass
class FeishuChannelConfig:
    app_id: str
    app_secret: str
    allowed_chats: list[str] = field(default_factory=list)  # 空=不限制


@dataclass
class _CardState:
    message_id: str
    content: str  # 累计内容


def _build_card(content: str, state_label: str = "") -> dict[str, Any]:
    """构建交互卡片 JSON。state_label 为空时为最终状态，不为空时带状态标签。"""
    header = {
        "title": {"tag": "plain_text", "content": "MonoX Agent"},
        "template": "blue" if not state_label else "grey",
    }
    if state_label:
        header["subtitle"] = {"tag": "plain_text", "content": state_label}

    elements = [
        {
            "tag": "markdown",
            "content": content or "_等待响应..._",
        }
    ]
    if state_label:
        elements.append({
            "tag": "hr",
        })
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": state_label}],
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": header,
        "elements": elements,
    }


class FeishuChannel:
    def __init__(
        self,
        cfg: FeishuChannelConfig,
        session_key: str = "default",
    ) -> None:
        self._cfg = cfg
        self._session_key = session_key
        self._sync_q: queue.Queue[InboundEvent | None] = queue.Queue()
        self._stop = asyncio.Event()
        self._ws_thread: threading.Thread | None = None
        self._started = threading.Event()
        self._open_id_by_chat: dict[str, str] = {}
        self._pending_session_key: str | None = None
        # 最近一次入站的真实 chat_id（reply 时按这个找 open_id）。
        # session_key 跨 channel 共享 default 后不再是 chat_id，所以 send 不能直接
        # 用 session_key 查 _open_id_by_chat，改用 listen 时记下的最近 chat_id。
        self._last_chat_id: str | None = None
        # 卡片状态：session_key → CardState
        self._card_by_session: dict[str, _CardState] = {}
        # 消息 ID 去重（飞书可能重试）
        self._seen_msg_ids: set[str] = set()
        self._lock = asyncio.Lock()
        #WS 线程的 event loop（供 _on_message 使用）
        self._ws_loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._ws_thread = threading.Thread(
            target=self._ws_run, daemon=True, name="feishu-ws"
        )
        self._ws_thread.start()
        self._started.wait(timeout=10)
        if not self._started.is_set():
            raise RuntimeError("Feishu WebSocket failed to start within 10s")

    def _ws_run(self) -> None:
        handler = (
            EventDispatcherHandler.builder(
                encrypt_key="",
                verification_token="",
            )
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        client = FeishuWSClient(
            app_id=self._cfg.app_id,
            app_secret=self._cfg.app_secret,
            log_level=LogLevel.INFO,
            event_handler=handler,
            auto_reconnect=True,
        )
        self._started.set()
        client.start()

    def _on_message(self, data) -> None:
        """在 lark-oapi WS 线程里被调用，直接 put_nowait 到 sync Queue。"""
        try:
            # 取 message_id 做去重
            message_id = getattr(data, 'message_id', None) or ""
            if message_id and message_id in self._seen_msg_ids:
                return
            if message_id:
                # 超过 1000 条清理一次
                if len(self._seen_msg_ids) > 1000:
                    self._seen_msg_ids.clear()
                self._seen_msg_ids.add(message_id)

            event = data.event
            if event is None:
                return
            sender = event.sender
            message = event.message
            if sender is None or message is None:
                return

            chat_id = message.chat_id or ""
            # sender.sender_id 可能为 None，单独判断
            sender_id_obj = getattr(sender, 'sender_id', None)
            open_id = ""
            if sender_id_obj is not None:
                open_id = getattr(sender_id_obj, 'open_id', "") or ""

            msg_type = message.message_type or ""
            content_str = message.content or "{}"

            if self._cfg.allowed_chats and chat_id not in self._cfg.allowed_chats:
                return

            if msg_type != "text":
                self._queue_reply(chat_id, open_id, f"[暂不支持 {msg_type} 消息类型]")
                return

            try:
                content = json.loads(content_str)
                text = content.get("text", "").strip()
            except (json.JSONDecodeError, TypeError):
                text = content_str.strip()

            if not text:
                return

            if open_id:
                self._open_id_by_chat[chat_id] = open_id

            import sys
            sys.stderr.write(
                f"[FeishuChannel] received: msg_id={message_id} chat={chat_id} "
                f"open_id={open_id} text={text!r}\n"
            )
            sys.stderr.flush()

            event_obj = InboundEvent(
                # 所有 feishu chat 都映射到 'default' —— 跨 channel 共享主会话
                # （MonoDesk / terminal / feishu 共用同一份对话历史）。
                # 真实 chat_id 保留在 meta 里，send 时按 _last_chat_id 找回 open_id。
                session_key="default",
                kind="message",
                text=text,
                source="feishu",
                event_type="user-input",
                timestamp=time.time(),
                meta={"chat_id": chat_id, "open_id": open_id, "message_id": message_id},
            )
            self._last_chat_id = chat_id
            self._sync_q.put_nowait(event_obj)
        except Exception as e:
            import sys
            sys.stderr.write(f"[FeishuChannel] _on_message error: {e}\n")
            sys.stderr.flush()

    def _queue_reply(self, chat_id: str, open_id: str, text: str) -> None:
        try:
            event_obj = InboundEvent(
                # 同上：所有 reply 都走 default session_key，跨 channel 共享
                session_key="default",
                kind="message",
                text=f"[auto-reply] {text}",
                source="feishu",
                event_type="system-notify",
                timestamp=time.time(),
                meta={"chat_id": chat_id, "open_id": open_id, "_auto_reply": True},
            )
            self._sync_q.put_nowait(event_obj)
        except Exception:
            pass

    async def stop(self) -> None:
        self._stop.set()
        if self._ws_thread:
            self._ws_thread.join(timeout=5)
        self._ws_thread = None

    async def listen(self) -> AsyncIterator[InboundEvent]:
        while not self._stop.is_set():
            while True:
                try:
                    event = self._sync_q.get_nowait()
                    self._pending_session_key = event.session_key
                    yield event
                except queue.Empty:
                    break
            await asyncio.sleep(0.05)

    # ----------------------------------------------------------------------
    # 发送逻辑：流式卡片
    # ----------------------------------------------------------------------

    async def send(self, event: StreamEvent) -> None:
        # 自动回复不走卡片
        if getattr(event, "_auto_reply", False):
            return

        # session_key 在跨 channel 共享 default 后不再是 chat_id。
        # 用最近一次入站的 chat_id 去找 open_id（reply 一定是对最近消息的回应）。
        # 兜底：如果 _last_chat_id 还没填（旧数据/非典型路径），退回用 _pending_session_key 当 chat_id。
        chat_id = self._last_chat_id or self._pending_session_key or self._session_key
        open_id = self._open_id_by_chat.get(chat_id, "")
        if not open_id:
            import sys
            sys.stderr.write(
                f"[FeishuChannel] no open_id for chat_id={chat_id}\n"
            )
            sys.stderr.flush()
            return

        if isinstance(event, StatusChange):
            if event.state == "thinking":
                await self._send_thinking_card(open_id, chat_id)
            return

        if isinstance(event, TokenChunk):
            await self._patch_card(chat_id, open_id, event.text, False)
            return

        if isinstance(event, FinalMessage):
            await self._patch_card(chat_id, open_id, event.text, True)
            return

        if isinstance(event, ErrorEvent):
            text = f"❌ {event.msg}"
            await self._patch_card(chat_id, open_id, text, True)
            return

    async def _send_thinking_card(self, open_id: str, session_key: str) -> None:
        """发出空白/思考中卡片，并记住 message_id。"""
        content = "_等待响应..._"
        card = _build_card(content, "💭 思考中")
        msg_id = await self._create_card_message(open_id, card)
        if msg_id:
            async with self._lock:
                self._card_by_session[session_key] = _CardState(
                    message_id=msg_id, content=content
                )

    async def _patch_card(
        self, session_key: str, open_id: str, new_text: str, is_final: bool
    ) -> None:
        """追加内容到卡片，或在 final 时替换为最终内容。"""
        card_state: _CardState | None = None
        async with self._lock:
            card_state = self._card_by_session.get(session_key)

        if not card_state:
            # 没有现有卡片（首次响应走 StatusChange），直接发最终消息
            if is_final:
                await self._send_text_message(open_id, new_text)
            return

        if is_final:
            # final：把卡片替换为最终内容，解除 thinking 状态
            content = new_text
            card = _build_card(content)
            async with self._lock:
                del self._card_by_session[session_key]
        else:
            # 追加 token 到卡片
            card_state.content += new_text
            content = card_state.content + "\n\n_继续生成中..._"
            card = _build_card(content, "💭 思考中")
            async with self._lock:
                self._card_by_session[session_key] = card_state

        await self._patch_card_message(card_state.message_id, card)

    # ----------------------------------------------------------------------
    # HTTP 请求（在线程池执行）
    # ----------------------------------------------------------------------

    async def _create_card_message(self, open_id: str, card: dict) -> str | None:
        """发交互卡片，返回 message_id。"""
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._http_create_card, open_id, card
            )
        except Exception as e:
            import sys
            sys.stderr.write(f"[FeishuChannel] _create_card error: {e}\n")
            sys.stderr.flush()
            return None

    async def _patch_card_message(self, message_id: str, card: dict) -> None:
        """PATCH 已发卡片内容。"""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, self._http_patch_card, message_id, card
            )
        except Exception as e:
            import sys
            sys.stderr.write(f"[FeishuChannel] _patch_card error: {e}\n")
            sys.stderr.flush()

    def _http_create_card(self, open_id: str, card: dict) -> str | None:
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest
            from lark_oapi.client import Client

            cli = Client.builder().app_id(self._cfg.app_id).app_secret(self._cfg.app_secret).build()
            body = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body({
                    "receive_id": open_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card),
                    "uuid": uuid.uuid4().hex,
                })
                .build()
            )
            resp = cli.im.v1.message.create(body)
            if resp.code == 0 and resp.data and resp.data.message_id:
                return resp.data.message_id
            import sys
            sys.stderr.write(f"[FeishuChannel] create_card failed: {resp.msg}\n")
            sys.stderr.flush()
            return None
        except Exception as e:
            import sys
            sys.stderr.write(f"[FeishuChannel] _http_create_card error: {e}\n")
            sys.stderr.flush()
            return None

    def _http_patch_card(self, message_id: str, card: dict) -> None:
        try:
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
            from lark_oapi.client import Client

            cli = Client.builder().app_id(self._cfg.app_id).app_secret(self._cfg.app_secret).build()
            body = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(PatchMessageRequestBody.builder()
                    .content(json.dumps(card))
                    .build())
                .build()
            )
            resp = cli.im.v1.message.patch(body)
            if resp.code != 0:
                import sys
                sys.stderr.write(f"[FeishuChannel] patch_card failed: {resp.msg}\n")
                sys.stderr.flush()
        except Exception as e:
            import sys
            sys.stderr.write(f"[FeishuChannel] _http_patch_card error: {e}\n")
            sys.stderr.flush()

    async def _send_text_message(self, open_id: str, text: str) -> None:
        """fallback：直接发文本消息（无卡片时不走卡片流程）。"""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._http_send_text, open_id, text)
        except Exception as e:
            import sys
            sys.stderr.write(f"[FeishuChannel] _send_text_message error: {e}\n")
            sys.stderr.flush()

    def _http_send_text(self, open_id: str, text: str) -> None:
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest
            from lark_oapi.client import Client

            cli = Client.builder().app_id(self._cfg.app_id).app_secret(self._cfg.app_secret).build()
            body = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body({
                    "receive_id": open_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}),
                })
                .build()
            )
            resp = cli.im.v1.message.create(body)
            if resp.code != 0:
                import sys
                sys.stderr.write(f"[FeishuChannel] send_text failed: {resp.msg}\n")
                sys.stderr.flush()
        except Exception as e:
            import sys
            sys.stderr.write(f"[FeishuChannel] _http_send_text error: {e}\n")
            sys.stderr.flush()
