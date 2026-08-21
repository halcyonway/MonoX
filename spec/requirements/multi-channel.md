# multi-channel: 多通道并行接入

> 依赖 `event-wrapper` 模块实现入站统一包装。

## 问题

当前 MonoX 只能跑一个 channel（`run.py build_channel` 只返回一个）。飞书和 terminal 不能同时开。

实际需求：
- 飞书远程发指令，看 terminal 输出（或者反过来）
- 多个飞书账号同时接入
- 共享同一个 memory / checkpoint（跨 channel 上下文不丢失）

## 设计目标

1. **多 channel 并行监听** — 同时接收多个 channel 的消息
2. **共享 loop / memory / checkpoint** — 跨 channel 上下文一致
3. **EventWrapper 统一协议** — 不同 channel 的消息包装成统一结构，注入 source / 时间 / 事件类型
4. **并行 fan-out** — agent 输出按 channel 分组，同时推送到多个 channel

---

## 架构

```
                    ┌──────────────────────────────────┐
                    │          run.py                 │
                    │  MultiChannelGateway            │
                    │    fan-in / fan-out             │
                    └──────────────┬──────────────────┘
                                   │
  feishu ──► InboundEvent ──► EventWrapper.wrap() ──► messages[]
  terminal ──► InboundEvent ──► EventWrapper.wrap() ──► messages[]
  scheduler ──► InboundEvent ──► EventWrapper.wrap() ──► messages[]
  webhook ───► InboundEvent ──► EventWrapper.wrap() ──► messages[]
                                   │
                            loop.run(input_q, output_q)
                                   │
                    ┌───────────────▼───────────────────┐
                    │          LoopEngine              │
                    │    (共享 memory/checkpoint)       │
                    └───────────────┬───────────────────┘
                                   │ FinalMessage(text)
                    ┌───────────────▼───────────────────┐
                    │     EventWrapper.parse_output()  │
                    │  <send channel="feishu">...</send> │
                    └───────────────┬───────────────────┘
                                   │ fan-out 并行
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
        FeishuChannel        TerminalChannel         SchedulerChannel
        .send(text)          .send(text)             .send(text)
```

### 关键设计点

1. **入站：EventWrapper 统一包装**
   - 各 signal source 产生 `InboundEvent`
   - `EventWrapper.wrap()` 转换成 `<event type="..." source="..." ts="..."><text>...</text></event>`
   - 包装后的文本加入 `messages[]`

2. **agent 输出：`<send>` 标签**
   - agent 在输出中嵌入 `<send channel="feishu">内容</send>`
   - `EventWrapper.parse_output()` 解析出所有标签
   - 没有标签时：回 pending_channel（单 channel 兼容）

3. **出站：fan-out 并行**
   - `MultiChannelGateway` 按解析结果并行调用各 channel 的 `.send()`
   - 不阻塞，不串行

4. **pending_channel 追踪**
   - 每次 `listen()` 取到 event 时记录 `pending_channel[session_key]`
   - 没有 `<send>` 标签时用这个

---

## 配置

```toml
[[channels]]
kind = "terminal"

[[channels]]
kind = "feishu"
enabled = true

[channels.feishu]
app_id = "cli_xxx"
app_secret = "xxx"

[channels.scheduler]
# 定时任务 channel（预留）
trigger = "0 9 * * *"
message = "每日定时推送"
```

启动时选择 channel：
```bash
uv run python run.py --channels=terminal,feishu
```

---

## 实现步骤

### Phase 1: EventWrapper 核心
- [x] `core/event_wrapper.py` 实现 `wrap()` + `parse_output()`
- [x] `InboundEvent` 加 `source`、`event_type`、`timestamp`
- [x] `MultiChannelGateway`（`core/gateway/multi.py`）：fan-in + fan-out
- [x] `run.py` 支持多 channel 构建

### Phase 2: 适配旧 channel
- [x] `TerminalChannel` 适配（`source="terminal"`, `event_type="user-input"`）
- [x] `FeishuChannel` 适配
- [x] `TextualChannel` 适配
- [x] 回归测试 68/68 通过

### Phase 3: 定时任务 channel
- [ ] `SchedulerChannel`：定时产生 `scheduled-task` 事件

---

## 风险

- `<send>` 标签 LLM 不一定会写 → system prompt 引导
- channel 数量多时 fan-out 并发量大 → channel.send() 要 async 无阻塞
- 多 channel 同时来消息 → loop 内部排队，按顺序处理
