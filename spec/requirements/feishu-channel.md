# feishu-channel: 飞书 IM 接入

## 问题

`extensions/channels/` 目前只有 `terminal.py` 和 `textual_chat.py`：

- terminal：本地交互，开 IDE / tmux 时用
- textual：全屏 TUI，本地体验好

远程 / 多端 / 手机用不上。需要 IM 通道。

飞书（[feishu.cn](https://www.feishu.cn/)）候选理由：
- 国内主流办公 IM
- 开放平台 bot 文档齐全（[open.feishu.cn](https://open.feishu.cn/)）
- WebSocket 长连接可用，避免用户搭 webhook 反向代理

## 现状（v0.9）

- 无 feishu 通道
- `Channel` Protocol 已支持任意新 channel 接入（start / stop / listen / send）

## 设计

### 长连接 vs Webhook

| 方案 | 优点 | 缺点 |
|---|---|---|
| WebSocket（飞书 SDK） | 无需公网 IP / 反代 | 长连接维护，断线重连 |
| Webhook | 简单，HTTP | 需要公网 + 反代 |

**选 WebSocket**：本地跑 MonoX 不需要暴露公网，对个人开发者友好。

### 协议映射

| 飞书事件 | MonoX InboundEvent |
|---|---|
| `im.message.receive_v1`（文本） | `kind="message"`，`text=msg.text` |
| `im.message.receive_v1`（post / image） | 暂不支持，先 echo 提示 |
| 私聊 | `session_key=chat_id`（每个私聊一个 session） |
| 群聊 @ bot | `session_key=chat_id`，`text` 去掉 @ 前缀 |

| MonoX StreamEvent | 飞书消息 |
|---|---|
| `FinalMessage` | 文本消息（合并 token chunk 后单发） |
| `ToolStart/ToolEnd` | 不发中间过程（避免刷屏），合并到 final |
| `ErrorEvent` | 文本消息（红色 emoji） |
| 思考 / debug 信息 | **不**发（避免 IM 噪音），只发 final |

> 简化：feishu channel **只**发 final message 和 error。中间过程走本地 terminal/textual。
> 理由：IM 端用户体验「等几秒看到结果」比「看着 token 流」更舒服。

### 鉴权

飞书 open platform 需要：

- `app_id` / `app_secret`：bot 身份
- `verification_token` / `encrypt_key`：消息签名（可选）

配置：

```toml
[channel]
kind = "feishu"

[channel.feishu]
app_id = "cli_xxx"
app_secret = "xxx"
encrypt_key = ""        # 可选
verification_token = ""  # 可选
allowed_chats = ["oc_xxx", "oc_yyy"]  # 白名单 chat_id（安全）
```

### 库选择

飞书官方 SDK `lark-oapi`（Python）覆盖 WebSocket：

```python
from lark_oapi.asynchronous import (
    AsyncLarkClient,
    AsyncEventDispatcherHandler,
    ws,
)
```

依赖加 `lark-oapi>=1.2`。

### 协议实现

```python
class FeishuChannel:
    def __init__(self, cfg: FeishuConfig):
        self._client = AsyncLarkClient(...)
        self._ws = ws.AsyncEventDispatcherHandler(...)
        self._in_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._out_q: asyncio.Queue[StreamEvent] = asyncio.Queue()

    async def start(self):
        await self._ws.start(self._client)

    async def stop(self):
        await self._ws.stop()

    async def listen(self):
        while not self._stop.is_set():
            try:
                yield await asyncio.wait_for(self._in_q.get(), timeout=0.5)
            except asyncio.TimeoutError: continue

    async def send(self, event: StreamEvent):
        # 只转发 final / error
        if isinstance(event, FinalMessage):
            await self._send_text(event.text)
        elif isinstance(event, ErrorEvent):
            await self._send_text(f"❌ {event.message}")
        # 其他：丢弃
```

### Inbound 监听

注册 handler 处理 `im.message.receive_v1`：

```python
@self._ws.on("im.message.receive_v1")
async def on_message(data: P2ImMessageReceiveV1):
    if data.header.chat_id not in self._cfg.allowed_chats:
        return
    text = data.event.message.content.text  # 已 strip @bot 前缀
    self._in_q.put_nowait(InboundEvent(
        session_key=data.header.chat_id,
        kind="message",
        text=text,
    ))
```

### 关闭流程

依赖 `requirements/shutdown.md` 的 shutdown_event：

- channel.stop() 时 SDK 长连接关闭
- engine 收到 shutdown_event 后退出 run loop

## 验证

- 单测：handler 把 mock 事件转 InboundEvent 正确（chat_id / text / session_key）
- 单测：send 只转发 FinalMessage / ErrorEvent，丢弃其他
- 手动：起一个 test bot + 用真飞书账号发消息 → 收到回复
- 回归：原有 e2e 4/4 不破

## 风险

- 飞书 SDK 依赖重（`lark-oapi` 依赖多）→ 装在 `extensions/`，core 仍干净
- 长连接断线（网络抖动 / 飞书侧重启）→ SDK 内置 reconnect + 自家 exponential backoff
- 消息发送频率限制 → final 单发，问题不大
- 群聊 bot 被 spam → `allowed_chats` 白名单
- 加密消息 → `encrypt_key` 配置项

## 进度

- 设计：本文档
- 实现：未开始（中期目标）