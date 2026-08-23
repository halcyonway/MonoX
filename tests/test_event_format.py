"""event_format.py 单元测试。

XML event 模型：user_input / tool_result 等外部事件以原始 XML 字符串送进
LLM context，含 ts / kind / channel 等属性。所有 assistant content 仍是
纯文本（OpenAI tool_call 格式不变）。
"""
from __future__ import annotations

import base64

import pytest

from core.loop.event_format import (
    EVENT_SCHEMA_DOC,
    interrupt_event_xml,
    tool_result_event_xml,
    user_input_event_xml,
)
from core.protocol import File, InboundEvent, ToolResult


# ---------- user_input_event_xml ----------

def test_user_input_basic_includes_required_attrs():
    ev = InboundEvent(
        session_key="default",
        kind="message",
        text="你好",
        source="monodesk",
        event_type="user-input",
        timestamp=1_700_000_000.5,
    )
    xml = user_input_event_xml(ev)
    assert 'kind="user_input"' in xml
    assert 'channel="monodesk"' in xml
    assert 'event_type="user-input"' in xml
    assert 'ts="1700000000.5"' in xml
    assert "你好" in xml
    assert xml.startswith("<event")
    assert xml.rstrip().endswith("</event>")


def test_user_input_renames_message_to_user_input():
    ev = InboundEvent(session_key="x", kind="message", text="hi")
    xml = user_input_event_xml(ev)
    assert 'kind="user_input"' in xml
    assert 'kind="message"' not in xml


def test_user_input_command_keeps_kind():
    ev = InboundEvent(session_key="x", kind="command", text="/reset", source="terminal")
    xml = user_input_event_xml(ev)
    assert 'kind="command"' in xml
    assert 'channel="terminal"' in xml


def test_user_input_escapes_special_chars():
    ev = InboundEvent(
        session_key="x", kind="message",
        text='<script>"hi" & goodbye</script>',
    )
    xml = user_input_event_xml(ev)
    # 所有特殊字符被 escape
    assert "<script>" not in xml
    assert "&lt;script&gt;" in xml
    assert '"hi"' not in xml  # 文本内容里的引号被 escape
    assert "&quot;" in xml or "&#x27;" in xml or "&#34;" in xml


def test_user_input_escapes_attr_value_quotes():
    ev = InboundEvent(
        session_key="x", kind="message", text="hi",
        event_type='bad"kind',
    )
    xml = user_input_event_xml(ev)
    # event_type attr 里的引号必须 escape
    assert 'event_type="bad&quot;kind"' in xml
    # 完整 XML 仍 well-formed
    import xml.etree.ElementTree as ET
    ET.fromstring(xml)


def test_user_input_attachment_emits_attachment_element():
    ev = InboundEvent(
        session_key="x", kind="message", text="see attached",
        attachments=(File(name="a.png", content=b"\x89PNG", mime="image/png"),),
    )
    xml = user_input_event_xml(ev)
    assert "<attachment" in xml
    assert 'name="a.png"' in xml
    assert 'mime="image/png"' in xml
    # base64 of b"\x89PNG"
    expected_b64 = base64.b64encode(b"\x89PNG").decode("ascii")
    assert expected_b64 in xml


def test_user_input_empty_text_and_no_attachments_self_closes():
    ev = InboundEvent(session_key="x", kind="message", text="")
    xml = user_input_event_xml(ev)
    # 没有 body 也没有 children → self-closing tag
    assert xml.endswith("/>") or xml.rstrip().endswith("/>")


def test_user_input_writes_zero_timestamp_attr():
    """timestamp=0.0 是合法值（epoch / unset），仍写出 ts="0"。"""
    ev = InboundEvent(session_key="x", kind="message", text="hi", timestamp=0.0)
    xml = user_input_event_xml(ev)
    assert 'ts="0"' in xml


# ---------- tool_result_event_xml ----------

def _ok_result(**kw) -> ToolResult:
    base = dict(call_id="c1", status="ok", stdout="hello", stderr="", exit_code=0)
    base.update(kw)
    return ToolResult(**base)


def test_tool_result_includes_required_attrs():
    r = _ok_result()
    xml = tool_result_event_xml("c1", r, tool="bash")
    assert 'kind="tool_result"' in xml
    assert 'tool="bash"' in xml
    assert 'call_id="c1"' in xml
    assert 'status="ok"' in xml
    assert 'exit_code="0"' in xml
    assert 'truncated="false"' in xml
    assert "<stdout>hello</stdout>" in xml
    assert "<stderr></stderr>" in xml


def test_tool_result_omits_tool_attr_when_not_provided():
    """不传 tool 时 attr 整体省略（不写 tool=""），避免 LLM 看到空值困惑。"""
    r = _ok_result()
    xml = tool_result_event_xml("c1", r)
    assert "tool=" not in xml


def test_tool_result_escapes_tool_name():
    r = _ok_result()
    xml = tool_result_event_xml("c1", r, tool='weird"tool')
    # 嵌套引号必须 escape
    assert 'tool="weird&quot;tool"' in xml
    import xml.etree.ElementTree as ET
    ET.fromstring(xml)


def test_tool_result_escapes_stdout_stderr():
    r = _ok_result(stdout='<a>&"</a>', stderr="<b>")
    xml = tool_result_event_xml("c1", r)
    assert "&lt;a&gt;" in xml
    assert "&amp;" in xml
    # 引号在 text node 里不用 escape 但 attr 里需要；stdout 是 text 内容
    assert '"&quot;' not in xml or "&amp;" in xml


def test_tool_result_truncated_with_budget_id():
    r = _ok_result(stdout="[L1 compressed]\nshort preview", truncated=True, budget_id="abc123")
    xml = tool_result_event_xml("c1", r)
    assert 'truncated="true"' in xml
    assert 'budget_id="abc123"' in xml


def test_tool_result_skips_budget_id_when_none():
    r = _ok_result()
    xml = tool_result_event_xml("c1", r)
    assert "budget_id" not in xml


def test_tool_result_with_artifacts_includes_attachments():
    r = _ok_result(
        artifacts=(File(name="out.txt", content=b"hello", mime="text/plain"),)
    )
    xml = tool_result_event_xml("c1", r)
    assert "<attachment" in xml
    assert 'name="out.txt"' in xml


# ---------- interrupt_event_xml ----------

def test_interrupt_no_event_renders_minimal():
    xml = interrupt_event_xml()
    assert xml == '<event kind="interrupt" />'


def test_interrupt_with_event_includes_channel():
    ev = InboundEvent(session_key="x", kind="interrupt", text="", source="monodesk", timestamp=42.0)
    xml = interrupt_event_xml(ev)
    assert 'kind="interrupt"' in xml
    assert 'channel="monodesk"' in xml
    assert 'ts="42' in xml


# ---------- EVENT_SCHEMA_DOC ----------

def test_schema_doc_documents_user_input_kind():
    assert "user_input" in EVENT_SCHEMA_DOC
    assert "channel=" in EVENT_SCHEMA_DOC
    assert "kind=" in EVENT_SCHEMA_DOC


def test_schema_doc_documents_tool_result_kind():
    assert "tool_result" in EVENT_SCHEMA_DOC
    assert "truncated" in EVENT_SCHEMA_DOC
    assert "budget_id" in EVENT_SCHEMA_DOC


def test_schema_doc_examples_are_well_formed():
    """文档里给的 code-block example 必须是 well-formed XML（防文档漂移）。

    只看 fenced ``` block，避开 prose 中夹杂的 `<event>` 引用。
    """
    import re
    import xml.etree.ElementTree as ET
    blocks = re.findall(r"```\s*\n([\s\S]*?)\n```", EVENT_SCHEMA_DOC)
    for b in blocks:
        m = re.search(r"<event\b[^>]*>[\s\S]*?</event>", b)
        if m:
            ET.fromstring(m.group(0))