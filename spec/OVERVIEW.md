# MonoX 架构概览

MonoX 是一个 Python agent runtime core，实现分层架构：

```
外部信号 ──► EventWrapper ──► LoopEngine ──► 输出路由
(signal)      (包装协议)     (推理/决策)    (fan-out)
```

---

## 核心模块

### `core/` — 稳定内核

零 UI / 零 IM / 零 LLM SDK 依赖。

| 模块 | 职责 |
|---|---|
| `protocol/` | 所有数据结构（`InboundEvent`、`StreamEvent`、`ToolResult` 等） |
| `channel/base.py` | `Channel` Protocol，所有 adapter 实现此协议 |
| `event_wrapper.py` | 外部信号统一包装（XML 格式），入站处理核心模块 |
| `gateway/` | `Gateway`（单 channel）、`MultiChannelGateway`（多 channel fan-in/fan-out） |
| `loop/` | `LoopEngine`（推理循环）、`ToolRegistry`、`Checkpoint`、`Memory` |
| `llm_proxy/` | OpenAI-compatible 流式接口 |
| `sandbox/` | `BashRunner`（安全执行 bash 工具） |
| `config.py` | 单一 config.toml 入口 |

### `extensions/` — 适配层

| 模块 | 职责 |
|---|---|
| `channels/` | 各 channel adapter（`terminal.py`、`feishu.py`、`textual_chat.py`） |
| `skills/` | 业务能力（shell 脚本） |

### `run.py` — 装配层

将 core + extensions 组装成完整 runtime。用户实际入口。

---

## 数据流

### 入站（外部 → agent）

```
外部信号（飞书、terminal、scheduler、webhook 等）
  │
  ▼
InboundEvent（source / event_type / timestamp / meta）
  │
  ▼
EventWrapper.wrap() → XML 字符串 → messages[]
  │
  ▼
LoopEngine（推理 + 决策）
```

### 出站（agent → 外部）

```
LoopEngine 输出 FinalMessage / ToolResult 等
  │
  ▼
EventWrapper.parse_output() 解析 <send channel="xxx"> 标签
  │
  ▼
MultiChannelGateway fan-out 并行推送
  │
  ▼
各 ChannelAdapter.send()
```

### MultiChannelGateway（多 channel）

```
多个 channel 并行 listen()
  │
  ▼  fan-in → 单一 input_q → LoopEngine
  │
  ▼  fan-out → parse_output()
  │
  ▼  并行路由到各 channel
  │
feishu ─ terminal ─ scheduler ─ webhook ...
```

---

## 关键协议

### Channel Protocol

所有 channel adapter 实现：

```python
class Channel(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def listen(self) -> AsyncIterator[InboundEvent]: ...
    async def send(self, event: StreamEvent) -> None: ...
```

### EventWrapper

所有外部信号通过 `EventWrapper` 统一包装：

```xml
<event type="user-input" source="feishu" ts="1732000000.123">
  <text>用户消息内容</text>
  <meta>
    <chat_id>oc_xxxx</chat_id>
  </meta>
</event>
```

### 出站路由

agent 输出中带 `<send>` 标签决定推送到哪个 channel：

```xml
<send channel="feishu">正在处理中...</send>
<send channel="terminal">也可以在 terminal 看实时输出</send>
```

---

## Session 与隔离

| 概念 | 说明 |
|---|---|
| `session_key` | 隔离维度，workspace / memory / checkpoint 按 session_key 隔离 |
| `checkpoint` | 每次 react 一步写一条 JSONL，服务重启可恢复 |
| `memory` | agent 的长期记忆（Memory.md），agent 自己读写 |

---

## 目录结构

```
MonoX/
├── core/
│   ├── protocol/        # 数据结构 + 事件 schema
│   ├── channel/base.py  # Channel Protocol
│   ├── event_wrapper.py  # 外部信号统一包装（入站核心）
│   ├── gateway/         # 双 pump 桥接
│   ├── loop/            # LoopEngine + tools + checkpoint
│   ├── llm_proxy/       # OpenAI 流式接口
│   ├── sandbox/         # BashRunner
│   └── config.py
├── extensions/
│   └── channels/        # channel adapter（飞书、terminal 等）
├── run.py              # 装配脚本
└── spec/
    ├── OVERVIEW.md     # 本文件
    ├── rule.md         # 开发规范
    ├── goal.md         # 长期目标
    └── requirements/   # 各需求方案
        ├── event-wrapper.md    # 入站信号统一包装
        ├── multi-channel.md   # 多通道并行接入
        ├── feishu-channel.md   # 飞书接入
        └── ...
```

---

## 设计原则

1. **core 稳定** — 不为单个 channel / LLM 妥协，零 UI 依赖
2. **Protocol 而非 ABC** — `runtime_checkable` 的 Protocol，任意 adapter 可接入
3. **事件不可变** — `@dataclass(frozen=True)`，所有 StreamEvent / InboundEvent
4. **入站统一包装** — 所有外部信号经过 EventWrapper，agent 不感知具体 channel
5. **出站路由显式** — `<send channel="xxx">` 标签，agent 决定推送到哪
