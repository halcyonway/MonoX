"""外部信号统一包装协议。

所有外部信号（channel / scheduler / webhook 等）经过 wrap() 统一包装成 XML 文本，
送入 LoopEngine 的 messages[]。出站时 parse_output() 解析 <send> 标签路由到各 channel。

XML 格式：
    <event type="user-input" source="feishu" ts="1732000000.123">
      <text>消息内容</text>
      <meta>
        <key>value</key>
      </meta>
    </event>
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Any

from core.protocol import InboundEvent


def _now() -> float:
    return time.time()


# ---------- 入站包装 ----------

def wrap(event: InboundEvent) -> str:
    """把 InboundEvent 转成 XML 字符串。"""
    ts = event.timestamp if event.timestamp else _now()

    # 拼顶层属性
    attrs = f'type="{_escape_attr(event.event_type)}" source="{_escape_attr(event.source)}" ts="{ts:.3f}"'

    parts = [f"<event {attrs}>"]

    # <text>
    parts.append(f"  <text>{_escape_text(event.text)}</text>")

    # <meta>（可选，有 key 时才写）
    if event.meta:
        parts.append("  <meta>")
        for k, v in event.meta.items():
            parts.append(f"    <{_xml_tag(k)}>{_escape_text(str(v))}</{_xml_tag(k)}>")
        parts.append("  </meta>")

    parts.append("</event>")
    return "\n".join(parts)


def _escape_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_tag(s: str) -> str:
    """把任意字符串转成合法的 XML tag 名（全小写，替换非法字符为 _）。"""
    tag = re.sub(r"[^a-zA-Z0-9_-]", "_", s).lower()
    return tag if tag and tag[0].isalpha() else f"_{tag}"


# ---------- 出站解析 ----------

# <send channel="feishu">内容</send>
_SEND_RE = re.compile(
    r'<send\s+channel\s*=\s*"([^"]+)"\s*>(.*?)</send>',
    re.DOTALL,
)


def parse_output(text: str, pending_channel: str) -> list[tuple[str, str]]:
    """从 agent 输出中解析 <send channel="xxx">...</send> 标签。

    Returns:
        [(channel, content), ...]，按出现顺序排列。
        没有 send 标签时返回 [(pending_channel, text)]（单 channel 兼容）。
    """
    matches = _SEND_RE.findall(text)
    if not matches:
        return [(pending_channel, text)]

    result = [(ch.strip(), content.strip()) for ch, content in matches]
    return result


# ---------- 调试用 ----------

def wrap_from_dict(
    text: str,
    source: str,
    event_type: str = "user-input",
    timestamp: float | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    """从原始字段生成 XML（不依赖 InboundEvent dataclass）。"""
    ts = timestamp if timestamp is not None else _now()
    event = InboundEvent(
        session_key="",
        kind="message",
        text=text,
        source=source,
        event_type=event_type,
        timestamp=ts,
        meta=meta or {},
    )
    return wrap(event)
