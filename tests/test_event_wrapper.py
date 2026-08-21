"""EventWrapper 单元测试。"""
from __future__ import annotations

import time
from core.event_wrapper import (
    wrap,
    parse_output,
    wrap_from_dict,
    _escape_text,
    _escape_attr,
    _xml_tag,
)
from core.protocol import InboundEvent


class TestWrap:
    def test_basic_user_input(self):
        e = InboundEvent(
            session_key="default",
            kind="message",
            text="hello",
            source="feishu",
            event_type="user-input",
            timestamp=1732000000.0,
        )
        xml = wrap(e)
        assert '<event type="user-input" source="feishu"' in xml
        assert "<text>hello</text>" in xml
        assert "</event>" in xml

    def test_timestamp_auto_fill(self):
        e = InboundEvent(
            session_key="default",
            kind="message",
            text="hi",
            source="terminal",
            event_type="user-input",
            timestamp=0.0,
        )
        xml = wrap(e)
        # 应该有 ts 属性且非零
        import re
        m = re.search(r'ts="([0-9.]+)"', xml)
        assert m is not None
        assert float(m.group(1)) > 0

    def test_meta_renders(self):
        e = InboundEvent(
            session_key="default",
            kind="message",
            text="hi",
            source="feishu",
            event_type="user-input",
            timestamp=1.0,
            meta={"chat_id": "oc_xxx", "open_id": "ou_yyy"},
        )
        xml = wrap(e)
        assert "<meta>" in xml
        assert "<chat_id>oc_xxx</chat_id>" in xml
        assert "<open_id>ou_yyy</open_id>" in xml

    def test_scheduled_task(self):
        e = InboundEvent(
            session_key="default",
            kind="message",
            text="daily trigger",
            source="scheduler",
            event_type="scheduled-task",
            timestamp=1732080000.0,
            meta={"trigger": "daily_9am"},
        )
        xml = wrap(e)
        assert 'type="scheduled-task"' in xml
        assert 'source="scheduler"' in xml

    def test_system_notify(self):
        e = InboundEvent(
            session_key="default",
            kind="message",
            text="channel disconnected",
            source="feishu",
            event_type="system-notify",
            timestamp=1.0,
        )
        xml = wrap(e)
        assert 'type="system-notify"' in xml

    def test_command(self):
        e = InboundEvent(
            session_key="default",
            kind="command",
            text="/debug on",
            source="terminal",
            event_type="command",
            timestamp=1.0,
        )
        xml = wrap(e)
        assert 'type="command"' in xml
        assert "<text>/debug on</text>" in xml

    def test_no_meta_omits_tag(self):
        e = InboundEvent(
            session_key="default",
            kind="message",
            text="hi",
            source="terminal",
            event_type="user-input",
            timestamp=1.0,
            meta={},
        )
        xml = wrap(e)
        assert "<meta>" not in xml

    def test_text_escaping(self):
        e = InboundEvent(
            session_key="default",
            kind="message",
            text="<script>alert('xss')</script>",
            source="feishu",
            event_type="user-input",
            timestamp=1.0,
        )
        xml = wrap(e)
        assert "<script>alert" not in xml
        assert "&lt;script&gt;" in xml

    def test_source_with_underscore(self):
        e = InboundEvent(
            session_key="default",
            kind="message",
            text="hi",
            source="feishu_channel_1",
            event_type="user-input",
            timestamp=1.0,
        )
        xml = wrap(e)
        assert 'source="feishu_channel_1"' in xml


class TestParseOutput:
    def test_single_send(self):
        out = 'hello <send channel="feishu">response text</send>'
        result = parse_output(out, "terminal")
        assert result == [("feishu", "response text")]

    def test_multiple_send(self):
        out = (
            '<send channel="feishu">feishu reply</send>'
            '<send channel="terminal">terminal reply</send>'
        )
        result = parse_output(out, "feishu")
        assert result == [
            ("feishu", "feishu reply"),
            ("terminal", "terminal reply"),
        ]

    def test_no_send_uses_pending(self):
        out = "just a plain response"
        result = parse_output(out, "feishu")
        assert result == [("feishu", "just a plain response")]

    def test_empty_send_returns_empty(self):
        out = '<send channel="feishu"></send>'
        result = parse_output(out, "terminal")
        assert result == [("feishu", "")]

    def test_send_with_whitespace(self):
        out = '<send channel="feishu">  content  </send>'
        result = parse_output(out, "terminal")
        assert result == [("feishu", "content")]

    def test_interleaved_text_ignored(self):
        out = 'hello <send channel="feishu">feishu reply</send> world <send channel="terminal">term reply</send>'
        result = parse_output(out, "default")
        assert result == [
            ("feishu", "feishu reply"),
            ("terminal", "term reply"),
        ]

    def test_unknown_channel_preserved(self):
        out = '<send channel="slack">slack msg</send>'
        result = parse_output(out, "feishu")
        assert result == [("slack", "slack msg")]


class TestWrapFromDict:
    def test_basic(self):
        xml = wrap_from_dict("hello", "feishu")
        assert "<text>hello</text>" in xml
        assert 'source="feishu"' in xml

    def test_with_event_type(self):
        xml = wrap_from_dict("daily", "scheduler", event_type="scheduled-task")
        assert 'type="scheduled-task"' in xml
        assert 'source="scheduler"' in xml

    def test_with_meta(self):
        xml = wrap_from_dict("hi", "feishu", meta={"chat_id": "oc_123"})
        assert "<chat_id>oc_123</chat_id>" in xml

    def test_with_timestamp(self):
        ts = 1732080000.0
        xml = wrap_from_dict("hi", "feishu", timestamp=ts)
        assert 'ts="1732080000.000"' in xml


class TestHelpers:
    def test_escape_text(self):
        assert _escape_text("a&b<c>d") == "a&amp;b&lt;c&gt;d"

    def test_escape_attr(self):
        assert _escape_attr('a"b<c>d') == "a&quot;b&lt;c&gt;d"

    def test_xml_tag_valid(self):
        assert _xml_tag("chat_id") == "chat_id"
        assert _xml_tag("openID") == "openid"
        assert _xml_tag("123abc") == "_123abc"

    def test_xml_tag_underscore_fallback(self):
        assert _xml_tag("") == "_"
        assert _xml_tag("123") == "_123"
