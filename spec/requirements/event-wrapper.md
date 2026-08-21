# event-wrapper: 外部信号统一包装协议

## 目标

**所有外部信号**（不限于 channel）经过 `EventWrapper` 统一包装成 XML 文本，送入 LoopEngine 的 `messages[]`。Agent 不直接处理原始用户消息，而是通过统一 event 接口感知外部世界。

外部信号包括：

| 来源 | 示例 | type |
|---|---|---|
| IM channel | 飞书/微信/Telegram 消息 | `user-input` |
| Terminal | 命令行输入 | `user-input` |
| 定时任务 | 每天 9 点触发 | `scheduled-task` |
| Webhook | 外部系统回调 | `webhook` |
| 系统通知 | channel 上线/断线 | `system-notify` |
| 命令 | `/debug`、`/clear` | `command` |
| 文件变化 | watchdog 监控 | `file-change` |
| Agent 间通信 | 其他 agent 发来消息 | `agent-msg` |
| Interrupt | Ctrl+C、shutdown | `interrupt` |

## 格式定义

### XML 结构

```xml
<event type="user-input" source="feishu" ts="1732000000.123">
  <text>用户消息内容</text>
  <meta>
    <chat_id>oc_xxxx</chat_id>
    <open_id>ou_xxxx</open_id>
  </meta>
</event>
```

```xml
<event type="scheduled-task" source="scheduler" ts="1732080000.000" trigger="daily_9am">
  <text>每日定时推送</text>
</event>
```

```xml
<event type="webhook" source="github" ts="1732000010.000">
  <text>GitHub webhook: push to main</text>
  <meta>
    <repo>owner/repo</repo>
    <branch>main</branch>
  </meta>
</event>
```

```xml
<event type="system-notify" source="feishu" ts="1732000010.000">
  <text>FeishuChannel disconnected, reconnecting...</text>
</event>
```

```xml
<event type="command" source="terminal" ts="1732000020.000">
  <text>/debug on</text>
</event>
```

```xml
<event type="file-change" source="watchdog" ts="1732000030.000">
  <text>.py文件变化: src/main.py</text>
  <meta>
    <path>src/main.py</path>
    <kind>modified</kind>
  </meta>
</event>
```

```xml
<event type="interrupt" source="system" ts="1732000040.000">
  <text>shutdown requested</text>
</event>
```

### 属性说明

| 属性 | 必须 | 说明 |
|---|---|---|
| `type` | 是 | 事件类型（见上方类型表） |
| `source` | 是 | 来源标识，channel 时为 channel 名，其他为来源模块名 |
| `ts` | 是 | Unix 时间戳（秒，浮点），精度到毫秒 |

### 子元素

| 元素 | 必须 | 说明 |
|---|---|---|
| `<text>` | 是 | 事件正文（纯文本） |
| `<meta>` | 否 | 扩展元数据，key-value 结构，由来源模块自行定义 |

`<meta>` 可以放任意子元素，结构由产生事件的信号源自行决定。

## 设计原则

1. **通用 XML** — 不绑定具体 channel，所有 channel 都用同一格式
2. **可扩展** — 新增 `type`、新增 `<meta>` 子元素，不需要改协议
3. **LLM 易解析** — `<event ...><text>...</text></event>` 结构清晰，system prompt 引导 LLM 提取
4. **与 LoopEngine 解耦** — `EventWrapper` 是独立模块，接收 `InboundEvent`，输出 XML 字符串

## 接口

```python
@dataclass(frozen=True)
class InboundEvent:
    session_key: str
    kind: Literal["message", "interrupt", "command", "attachment"]
    text: str
    channel: str = "default"
    event_type: str = "user-input"       # 新增
    timestamp: float = 0.0               # 新增
    attachments: tuple[File, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


class EventWrapper:
    @staticmethod
    def wrap(event: InboundEvent) -> str:
        """把 InboundEvent 转成 XML 字符串。"""

    @staticmethod
    def parse_output(text: str, pending_channel: str) -> list[tuple[str, str]]:
        """从 agent 输出中解析 <send channel="xxx">...</send> 标签。
        返回 [(channel, content), ...]。
        没有 send 标签时返回 [(pending_channel, text)]。
        """
```

## `<send>` 标签（出站路由）

agent 输出中带 `<send>` 标签表示要往哪个 channel 发消息：

```xml
好的，我来帮你查一下。
<send channel="feishu">正在查找代码...</send>
<send channel="terminal">也可以在 terminal 看：find . -name "*.py"</send>
```

解析后并行 fan-out 到各 channel。没有 `<send>` 标签时，回 `pending_channel`（单 channel 兼容）。

## 实现位置

`core/event_wrapper.py`（独立模块，不属于任何 channel）

## 进度

- [x] `InboundEvent` 加 `source`、`event_type`、`timestamp`
- [x] `core/event_wrapper.py` 实现 `wrap()` + `parse_output()`
- [x] `TerminalChannel` 适配（`source="terminal"`, `event_type="user-input"`）
- [x] `FeishuChannel` 适配（`source="feishu"`, `event_type="user-input"/"system-notify"`）
- [x] `TextualChannel` 适配（`source="textual"`, `event_type="user-input"`）
- [x] 回归测试 58/58 通过
- [ ] MultiChannelGateway 实现（fan-in / fan-out）
