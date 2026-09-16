"""Event → XML 序列化。

设计原则：所有外部信息（user_input / tool_result / command / interrupt）
统一用 XML 格式送到 LLM context，至少含 timestamp + channel 等元数据。

进 LLM context 的 messages 里，所有「外部信息」（user_input / tool_result /
command / interrupt）都用 XML 格式承载，至少含：
- ts (Unix timestamp 秒，浮点)
- kind (user_input / tool_result / ...)
- channel / event_type (来源标识)

assistant content 保持纯文本（agent 自己的输出）；tool_calls 仍是 OpenAI
tool_call 格式（API 强约束）。

XML schema：
  <event ts="1700000000.5" kind="user_input" channel="monodesk"
         event_type="user-input">
    你好
  </event>

  <event ts="1700000000.5" kind="tool_result" tool="bash" status="ok"
         exit_code="0" budget_id="abc" truncated="false">
    <stdout>hi</stdout>
    <stderr></stderr>
  </event>
"""
from __future__ import annotations

import html
from typing import Any

from core.protocol import File, InboundEvent, ToolResult


def _escape(s: str) -> str:
    """XML text + attr 双重安全 escape（& / < / > / " / '）。"""
    return html.escape(s, quote=True)


def _attrs(pairs: dict[str, Any]) -> str:
    """dict → ' k1="v1" k2="v2"'（None 值跳过，bool 转 true/false）。"""
    out: list[str] = []
    for k, v in pairs.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, float):
            # 保持精度但不写指数
            v = f"{v:.6f}".rstrip("0").rstrip(".")
            if not v:
                v = "0"
        out.append(f' {k}="{_escape(str(v))}"')
    return "".join(out)


# ---- 各 event kind 的 XML renderer ----

def user_input_event_xml(ev: InboundEvent) -> str:
    """user_input / command → XML event。

    command 也走同一个 schema（kind=command）以保持 event 流统一。
    """
    body = _escape(ev.text or "")
    head = _attrs({
        "ts": ev.timestamp or 0,
        "kind": ev.kind if ev.kind != "message" else "user_input",
        "channel": ev.source,
        "event_type": ev.event_type,
    })
    # attachments 单独成 children（base64 content）
    children = ""
    for f in ev.attachments:
        children += _attachment_xml(f)
    if children:
        return f"<event{head}>\n{body}\n{children}\n</event>"
    if body:
        return f"<event{head}>\n{body}\n</event>"
    return f"<event{head} />"


def interrupt_event_xml(ev: InboundEvent | None = None) -> str:
    """interrupt → 空 event（无 payload，仅元数据）。"""
    if ev is None:
        return '<event kind="interrupt" />'
    head = _attrs({
        "ts": ev.timestamp or 0,
        "kind": "interrupt",
        "channel": ev.source,
    })
    return f"<event{head} />"


def tool_result_event_xml(call_id: str, result: ToolResult, *, tool: str | None = None) -> str:
    """tool result → XML event（role=tool 消息的 content）。

    `tool` 是 caller 注入的 tool 名（如 "bash" / "wait_io"），写进 event attr；
    不传时 attr 整体省略（不让 LLM 看到 `tool=""` 这种无意义空串）。
    """
    head = _attrs({
        "kind": "tool_result",
        "tool": tool,
        "call_id": call_id,
        "status": result.status,
        "exit_code": result.exit_code,
        "truncated": result.truncated,
        "budget_id": result.budget_id,
    })
    stdout = _escape(result.stdout)
    stderr = _escape(result.stderr)
    body = f"<stdout>{stdout}</stdout>\n<stderr>{stderr}</stderr>"
    if result.artifacts:
        for f in result.artifacts:
            body += "\n" + _attachment_xml(f)
    return f"<event{head}>\n{body}\n</event>"


def _attachment_xml(f: File) -> str:
    """File → <attachment> element.

    - File.path 存在 → 渲染 `path` 属性（本地绝对路径，LLM 拿来直接调 read_doc）。
    - 否则 File.content 当 URL / 路径字符串 → 渲染 `url` 属性（向后兼容）。
    - 否则 base64 编码 File.content 嵌入 body。
    """
    if f.path:
        head = _attrs({"name": f.name, "mime": f.mime, "path": f.path})
        return f"<attachment{head} />"

    url_bytes = f.content or b""
    try:
        url_str = url_bytes.decode("utf-8")
        if url_str.startswith("http://") or url_str.startswith("https://") or url_str.startswith("/"):
            head = _attrs({"name": f.name, "mime": f.mime, "url": url_str})
            return f"<attachment{head} />"
    except UnicodeDecodeError:
        pass

    import base64
    b64 = base64.b64encode(url_bytes).decode("ascii")
    head = _attrs({"name": f.name, "mime": f.mime})
    return f"<attachment{head}>{_escape(b64)}</attachment>"


# ---- 给 LLM 的 schema 文档（嵌入 system prompt）----

EVENT_SCHEMA_DOC = """\
## Event schema in context

Every external event arriving in `user` or `tool` messages is wrapped in XML so
you have explicit context about timing and source. Treat each `<event>` as one
discrete occurrence in the conversation log, not a free-form message.

User-side events:
  <event ts="1700000000.5" kind="user_input" channel="monodesk"
         event_type="user-input">
    user message body
    <attachment name="meeting.m4a" mime="audio/mp4" url="/Users/.../meeting.m4a" />
  </event>
  - `ts`: Unix seconds (float). Use to compute latency / ordering.
  - `kind`: user_input (interactive), command (slash command like /reset),
    system (runtime-generated notification — NOT typed by the user; e.g.
    async-task results from your fork_task background tasks).
  - `channel`: source identifier (monodesk / terminal / async_task / etc.).
  - `event_type`: sub-classification (user-input / scheduled-task /
    async-task-result / ...).
  - `<attachment>`: file reference. `name` is the original filename;
    `mime` is MIME (e.g. `image/png`, `application/pdf`); `path` is the
    LOCAL absolute path on disk — pass it directly to `read_doc(path=...)`
    or `multimodalunderstand(attachment_url=...)`. Do not pass URLs.

Tool-side events (in `role=tool` messages):
  <event kind="tool_result" call_id="c1" status="ok" exit_code="0"
         truncated="false" budget_id="...">
    <stdout>command output</stdout>
    <stderr>error output if any</stderr>
  </event>
  - `truncated=true` + `budget_id`: L1 compression applied; call
    read_tool_result_budget(budget_id) to fetch the full version.
  - `status` ∈ ok / error / timeout / cancelled.

Assistant content (your own prior replies) is plain text, not XML-wrapped. \
Tool calls (when you choose to call a function) are returned via the standard \
tool_calls field, not inside <event>."""