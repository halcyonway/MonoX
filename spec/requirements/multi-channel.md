# multi-channel: 多通道并行接入

> **已迁移** — 本文档描述的 in-process `MultiChannelGateway` 已被 Runtime↔Gateway
> 进程级解耦方案替代。新拓扑详见 `../ARCHITECTURE.md` 第 12 节。
>
> 本文档保留作为历史设计参考；旧 in-process 装配代码已彻底删除
> （Runtime↔Gateway 解耦后不再需要单进程兼容路径）。

## 问题

MonoX 单进程模式下 channel 与 core 耦合：飞书、terminal、desktop 客户端不能在
Runtime 进程外独立启停；channel-specific 依赖（lark-oapi / textual / rich）被
core 启动时强制拉起。

实际需求：

- 飞书远程发指令，看 terminal 输出（或者反过来）
- 多个飞书账号同时接入
- 共享同一个 memory / checkpoint（跨 channel 上下文不丢失）
- channel 可以独立启停、独立升级

## 设计目标

1. **多 channel 并行监听** — 同时接收多个 channel 的消息
2. **共享 loop / memory / checkpoint** — 跨 channel 上下文一致
3. **EventWrapper 统一协议** — 不同 channel 的消息包装成统一结构，注入 source / 时间 / 事件类型
4. **并行 fan-out** — agent 输出按 channel 分组，同时推送到多个 channel
5. **channel 与 core 进程级解耦** — channel 独立进程 / 独立启停

---

## 当前实现：Runtime↔Gateway 双进程

新方案由 Runtime 进程和 Gateway 进程两个独立单元组成：

- **Runtime 进程**（`run.py`）：只起 LoopEngine + RuntimeServer（ws server）
- **Gateway 进程**（`extensions/gateway/`）：拉起多个 channel adapter + 连 Runtime ws

channel 仍走 EventWrapper `<send channel="...">` 标签解析分发；Routing 由
Gateway 持有；Runtime 完全不知道 channel 是什么。

详见 [ARCHITECTURE.md §12](../ARCHITECTURE.md#12-runtime--gateway-进程级解耦)。

---

## 历史架构（已废弃，保留作为参考）

> 以下描述单进程 `MultiChannelGateway` 方案。代码已彻底删除。

```
                    ┌──────────────────────────────────┐
                    │          run.py                 │
                    │  MultiChannelGateway            │
                    │    fan-in / fan-out             │
                    └──────────────┬───────────────────┘
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

### 关键设计点（仍适用于新方案）

1. **入站：EventWrapper 统一包装**（保持不变）
2. **agent 输出：`<send>` 标签**（保持不变；Gateway 端 FanOut 用 `parse_output`）
3. **出站：fan-out 并行**（保持不变）
4. **pending_channel 追踪**（新方案由 Gateway `RoutingTable` 承担）

---

## 配置

### Runtime 配置（`config.toml`）

```toml
[server]
host = "127.0.0.1"
port = 8765
```

### Gateway 配置（CLI）

```bash
uv run python -m extensions.gateway \
    --channels=monodesk,terminal,feishu \
    --runtime-url=ws://127.0.0.1:8765 \
    --session-key=default \
    --model=gpt-4
```

旧 `[[channels]]` 配置块在 Runtime 模式下不消费。

---

## 实现状态

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

### Phase 3: Runtime↔Gateway 进程级解耦（取代原 Phase 3）
- [x] `core/protocol/wire_frames.py` — 13 个 FrameType + 双向转换
- [x] `core/runtime_server.py` — Runtime 端 ws server
- [x] `core/config.py` — `[server]` 段
- [x] `extensions/gateway/` — 独立进程（含 ws client / RoutingTable / FanOut）
- [x] `extensions/channels/monodesk.py` — in-process adapter 形态
- [x] `run.py` — runtime-only，删除 build_channels
- [x] 157 / 157 测试通过

### Phase 4: 后续工作
- [ ] 给 `FinalMessage` / `ErrorEvent` 加 `session_key` 字段，启用真正的 session 路由
- [ ] feishu / terminal / textual 在 Gateway 模式下的端到端集成测试

---

## 风险

- `<send>` 标签 LLM 不一定会写 → system prompt 引导
- channel 数量多时 fan-out 并发量大 → channel.send() 要 async 无阻塞
- 多 channel 同时来消息 → loop 内部排队，按顺序处理
- Runtime↔Gateway 进程断连 → 指数退避重连；未发的上行帧可能在重连间隙丢失
  （当前不持久化 out_q；下一轮可加重连后 replay）
- Runtime seq 重启后从 0 重计 → desktop 客户端可能短暂观察到 seq 跳变
  （客户端按 session_key 重新同步 session 状态即可）