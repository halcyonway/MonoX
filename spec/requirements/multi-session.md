# multi-session: Runtime 多 session 化 + channel 独立进程化

## 问题

`spec/ARCHITECTURE.md` §12 当前架构：单 Runtime 进程 = 单 `LoopEngine`（默认 session_key="default"）+ 单 `RuntimeServer`；channel 由 `extensions/gateway/` 进程拉起并通过 ws client 连 Runtime。

**不能用的场景**：

- monoDesk 用户想开**第二个独立会话**（vs 默认主 session）—— 当前 Runtime 单 LoopEngine 无法支持
- feishu **多个群**同时接入—— 群id 各异但 Runtime 只服务一个 session_key
- terminal + monoDesk 共享同一桌面（开发者在 IDE 里用 terminal 操作，复盘桌面输出）—— 两个 channel 共用 "default"，但每端期望**只看到自己触发的对话**

## 目标与边界

### 目标

1. **Runtime 多 session**：每个 `session_key` 一个独立 `LoopEngine` 实例，idle 超时销毁 + checkpoint 恢复
2. **session_key 通用**：feishu 群id、monodesk 会话 id、terminal "default" 都是 `session_key` 的实例——Runtime 不区分
3. **channel 独立进程**：每个 channel 单独启停 / 单独升级；Runtime 对 channel 协议零感知
4. **fan-out 单 conn**：同一 session_key 多个 channel 共享时，Runtime 下行事件只发给最近活跃的那一端（用户已确认选项 B）
5. **HTTP `/health`**：列出当前 Runtime 服务的活跃 sessions

### 边界

- **不改** `core/loop/engine.py`（`_restore` 已支持 lazy create）
- **不改** `core/channel/base.py` / `core/memory/` / `core/llm_proxy/` / `core/sandbox/` / `core/config.py`
- **不改** `hello_frame` 函数签名（monoDesk adapter ↔ desktop 通信也用它，不需要 source）
- **删** `extensions/gateway/` 整包（被 channel 独立进程替代）
- **FinalMessage 不加 session_key 字段**（Runtime 端按 output_q 归属 + ws conn 归属决定路由，不需要 schema 变更）

---

## 设计

### 进程拓扑

```
+──────────────────────────────────────────────────────────────────+
│                          Runtime 进程                              │
│                                                                  │
│   ┌────────────────────┐  ┌──────────────────────┐                │
│   │ RuntimeServer      │  │ SessionManager       │                │
│   │  :8765 (ws)        │◄─┤  dict[sk → Session]  │                │
│   │  clients:          │  │  - lazy create       │                │
│   │   (sk, src) → ws   │  │  - idle sweep        │                │
│   │  register_session  │  │  - clock seam        │                │
│   │   (sk, output_q)   │  └──────────┬───────────┘                │
│   └──────────┬─────────┘             │                            │
│              │                       │  per-session                │
│              ▼                       ▼  LoopEngine                 │
│   ┌─────────────────────────────────────────────────┐            │
│   │ SessionLoop[sk]                                  │            │
│   │  - LoopEngine (per sk, restored from ckpt)      │            │
│   │  - input_q / output_q                            │            │
│   │  - last_active_ts                                │            │
│   └─────────────────────────────────────────────────┘            │
│                                                                  │
│   ┌────────────────────┐                                          │
│   │ HealthServer       │  :8767  GET /health → {"sessions":[...]}│
│   └────────────────────┘                                          │
└──────────────────────────────────────────────────────────────────┘
        ▲            ▲            ▲              ▲
        │ ws         │ ws         │ ws           │ ws
   ┌────┴────┐  ┌────┴────┐  ┌────┴────┐  ┌──────┴──────┐
   │ monodesk│  │ terminal│  │  feishu │  │  textual   │
   │ 进程    │  │  进程   │  │  进程   │  │   进程      │
   │ ws:8766 │  │  stdio  │  │ lark WS │  │   TUI       │
   │ (desktop│  │         │  │  +HTTP  │  │             │
   │  客户端)│  │         │  │         │  │             │
   └─────────┘  └─────────┘  └─────────┘  └─────────────┘
```

每个 channel 进程：
- hello 帧带 `data.source`（"monodesk" / "terminal" / "feishu" / "textual"）+ `data.session_key`（默认 "default"，或自己的语义 key）
- Runtime 用 `(session_key, source)` 索引 ws conn
- inbound → 按 `ev.session_key` 派发到对应 `SessionLoop.input_q` + 更新 `last_active_source[sk]`
- outbound（来自 SessionLoop.output_q）→ RuntimeServer 按 `(sk, last_active_source[sk])` 路由

### SessionManager

```python
@dataclass
class SessionLoop:
    session_key: str
    loop_engine: LoopEngine
    input_q: asyncio.Queue[InboundEvent]
    output_q: asyncio.Queue[StreamEvent]
    task: asyncio.Task | None
    last_active_ts: float

class SessionManager:
    IDLE_TIMEOUT_SEC = 300
    SWEEP_INTERVAL_SEC = 30

    def __init__(self, *, llm, tools, compression, state_root, traces_root,
                 time_fn=time.time, idle_timeout_sec=IDLE_TIMEOUT_SEC,
                 sweep_interval_sec=SWEEP_INTERVAL_SEC): ...

    async def dispatch_inbound(self, ev: InboundEvent) -> None:
        sl = self._sessions.get(ev.session_key)
        if sl is None:
            sl = self._create(ev.session_key)        # lazy create + 从 checkpoint 恢复
            self._sessions[ev.session_key] = sl
            await sl.start()
        sl.last_active_ts = self._time_fn()
        await sl.input_q.put(ev)

    def register_session_listener(self, session_key, on_event) -> None:
        """让外部（RuntimeServer）订阅某 session 的 output_q。
        SessionManager 持有 (session_key, callback) 列表。"""
        ...

    def active_sessions(self) -> list[str]: ...

    async def _idle_sweeper(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._sweep_interval_sec)
            now = self._time_fn()
            for sk, sl in list(self._sessions.items()):
                if (now - sl.last_active_ts) > self._idle_timeout_sec:
                    await sl.destroy()
                    del self._sessions[sk]
```

**per-session 资源**：
- `JsonlCheckpointStore(state_root / session_key / "checkpoint.jsonl")` — 每 session 一份（多 session 不能共享一个 jsonl 文件，落在独立 state root —— Runtime 内部 state，跟 LLM shell cwd `workspace_root` 严格隔离）
- `JsonlTraceStore(traces_root / session_key / "traces.jsonl")` — 每 session 一份，落在独立 traces root
- `FsMemoryStore(memory_root)` — 共享实例（跨会话全局）
- `LLMProxy` / `ToolRegistry` / `CompressionService` — 共享（无 session 状态）

### RuntimeServer

```python
class RuntimeServer:
    def __init__(self, cfg, *, default_session_key="default", default_source="unknown"): ...

    async def run(self) -> None: ...               # websockets.serve + manage per-session consumer tasks

    def register_session_listener(self, session_key, on_event) -> None:
        """外部（SessionManager）注册 session_key → callback。
        RuntimeServer 启动一个 consumer task 把对应 SessionLoop.output_q 的
        event 按 last_active_source 路由到目标 ws。"""
        ...

    def unregister_session_listener(self, session_key) -> None: ...

    # _clients: dict[(session_key, source), WebSocket]
    # _last_active_source: dict[session_key, source]
    # _session_listeners: dict[session_key, Callable[[StreamEvent], Awaitable]]
```

**hello 帧 schema（Runtime 端）**：

```json
{"v":1, "type":"hello", "seq":0, "ts":0,
 "data":{
   "session_key":"default",
   "model":"gpt-4",
   "source":"monodesk"        // ← 新增
 }}
```

`source` 由 `RuntimeWSClient` 内联构造 hello dict 时填入（不改 `hello_frame` 公共函数签名）。

**`_on_connect` 解析**：

```python
if mtype == "hello":
    session_key = first_payload["data"].get("session_key") or default_session_key
    source = first_payload["data"].get("source") or default_source
else:
    # 第一帧是 inbound（兼容 monoDesk）：用 default_session_key + default_source
    session_key, source = default_session_key, default_source
    first_ev = from_frame(first_payload, default_session_key=session_key, default_source=source)
```

**fan-out 路由**（用户已确认选项 B）：

```python
# RuntimeServer 持有的每 session_key consumer task：
async def _session_output_consumer(self, session_key, output_q):
    while not self._stop.is_set():
        ev = await output_q.get()
        src = self._last_active_source.get(session_key)
        if src is None:
            continue                  # 无活跃 source → 丢弃
        ws = self._clients.get((session_key, src))
        if ws is None:
            continue
        frame = to_frame(ev, seq=next(self._seq))
        if frame is None:
            continue
        await ws.send(json.dumps(frame, ...))
```

**disconnect 时清理**：

```python
# 在 _on_connect 的 finally 块中
if self._last_active_source.get(session_key) == source:
    # 检查同 session_key 还有没有该 source 的其他 conn
    has_other = any(
        sk == session_key and src == source and ws is not conn
        for (sk, src), conn in self._clients.items()
    )
    if not has_other:
        self._last_active_source.pop(session_key, None)
```

### ws wire 改动

**入站帧**（channel 进程 → Runtime）：保持 3 type：`user_input` / `command` / `interrupt`。`from_frame` 的 `default_source` 参数由 `RuntimeServer._on_connect` 传入（来自 hello 或 `default_source`）。

**出站帧**（Runtime → channel 进程）：保持 10 type。`to_frame(ev, seq)` 不变。

### HTTP Health

```python
# core/health_server.py
class HealthServer:
    def __init__(self, *, session_manager, host="127.0.0.1", port=8767): ...

    async def run(self) -> None:
        server = await asyncio.start_server(self._handle, host, port)
        async with server:
            await server.serve_forever()

    async def _handle(self, reader, writer) -> None:
        # 读 HTTP request line + headers（足够少，简单实现）
        # GET /health → 200 + JSON {"sessions": [...]}
        # 其他路径 → 404
        ...
```

### channel 进程 mini-runtime 模板

```python
# extensions/channels/monodesk/__main__.py
async def _async_main(cfg):
    ch = MonoDeskChannel(MonoDeskChannelConfig(host="127.0.0.1", port=8766, model=cfg.model),
                         session_key=cfg.session_key)
    await ch.start()
    ws_client = RuntimeWSClient(
        url=cfg.runtime_url,
        hello_session_key=cfg.session_key,
        hello_source="monodesk",          # ← 硬编码 source
    )
    ws_task = asyncio.create_task(ws_client.run())
    try:
        await asyncio.gather(
            pump_inbound(ch, ws_client),    # ch.listen() → ws_client.send(frame)
            pump_outbound(ch, ws_client),   # ws_client.recv() → ch.send(ev)
        )
    finally:
        await ws_client.stop()
        ws_task.cancel()
        await ch.stop()

def pump_inbound(ch, ws_client):
    async def gen():
        async for ev in ch.listen():
            frame = inbound_to_frame(ev, seq=0)
            if frame is not None:
                await ws_client.send(frame)
    return gen()

def pump_outbound(ch, ws_client):
    async def gen():
        while True:
            ev = await ws_client.recv()
            await ch.send(ev)
    return gen()
```

**不引入独立 probe**：RuntimeWSClient 启动时 Runtime 未起 → 进入 backoff 重连；不是崩溃。砍掉原提议的「probe 失败 exit 1」。

---

## 验证

### 单元测试

`tests/test_session_manager.py`：
```python
async def test_dispatch_creates_session_on_first_inbound(): ...
async def test_dispatch_reuses_existing_session(): ...
async def test_idle_sweeper_destroys_session(): ...        # 用 injected clock
async def test_destroy_then_redispatch_recreates(): ...
async def test_per_session_checkpoint_isolation(): ...     # 两个 session 不互相覆盖
```

`tests/test_runtime_server.py`（更新）：
```python
async def test_hello_with_source_registers_under_pair(): ...
async def test_two_sources_same_session_key_both_kept(): ...
async def test_same_source_reconnect_replaces(): ...
async def test_fanout_uses_last_active_source_only(): ...
async def test_dead_conn_clears_last_active_source(): ...
async def test_register_session_listener_consumes_per_session(): ...
```

`tests/test_health.py`：
```python
async def test_get_health_returns_active_sessions(): ...
async def test_unknown_path_returns_404(): ...
```

`tests/test_runtime_ws_client.py`（新，从 test_gateway.py 拆）：
```python
async def test_hello_includes_source(): ...
async def test_reconnect_after_disconnect(): ...
async def test_round_trip(): ...
```

### 端到端手动

```bash
# 终端 0：Runtime
uv run python run.py
# → :8765 ws 监听，:8767 health 监听

# 终端 1：monoDesk channel
uv run python -m extensions.channels.monodesk \
    --runtime-url=ws://127.0.0.1:8765 --session-key=default
# → :8766 监听给 desktop client，ws client 连 Runtime

# 终端 2：terminal channel
uv run python -m extensions.channels.terminal \
    --runtime-url=ws://127.0.0.1:8765 --session-key=default
# → prompt-toolkit stdio；ws client 连 Runtime

# 验证：
# 1. desktop 发 user_input → terminal 终端不显示（last_active 是 monodesk）
# 2. terminal 输入文本 → desktop 客户端不显示（last_active 是 terminal）
# 3. curl http://127.0.0.1:8767/health → {"sessions": ["default"]}
# 4. --idle-timeout=10 起 Runtime → 10 秒无活动 → curl /health 应不含该 session
```

---

## 风险

- **多 session checkpoint 写入并发**：每 session 一份 jsonl 文件，无共享写，无 race
- **`last_active_source` 在 conn 断开后短暂不一致**：disconnect → consumer task 检测到 ws 死了，下次 event 路由失败丢弃；不持久化任何状态
- **`wait_io` 后的 idle 计时**：wait_io 后 input_q 空 + output_q 静默 → idle_since 起算；新 inbound 触发重建 + 从 checkpoint 恢复 messages
- **`SessionLoop` task cancel 时机**：destroy 中 cancel → task 抛 CancelledError → 已在 finally 中处理；下次 `_create` 走完整 `_restore` 路径
- **Session metric 不持久**：`_session_metric` 当前不写 checkpoint，重建后归零——已知限制
- **clock seam 测试覆盖率**：idle sweep 测试用 `time_fn` 注入 fake clock，避免 sleep 5 分钟

---

## 进度

### Phase 1: spec + 抽 RuntimeWSClient + 公共基础设施
- [x] `spec/requirements/multi-session.md`
- [ ] `core/runtime_ws_client.py`（从 extensions/gateway 抽）
- [ ] `RuntimeWSClient` 加 `hello_source` 入参；hello dict 内联构造

### Phase 2: RuntimeServer 改造 + SessionManager
- [ ] `core/runtime_server.py` `_clients: dict[(sk, src), ws]`；`register_session_listener` API；`last_active_source` 维护 + disconnect 清理
- [ ] `core/session_manager.py` `SessionLoop` + `SessionManager`（含 idle sweep + clock seam）

### Phase 3: health + run.py + channel 独立进程化
- [ ] `core/health_server.py` stdlib HTTP `/health`
- [ ] `run.py` 装配 SessionManager + HealthServer；删单 LoopEngine 装配；CLI 加 `--health-port` / `--idle-timeout`
- [ ] `extensions/channels/{monodesk,terminal,feishu,textual_chat}/__main__.py`
- [ ] 删 `extensions/gateway/` 整包
- [ ] 删 `tests/test_gateway.py`

### Phase 4: 测试 + 文档
- [ ] `tests/test_session_manager.py`
- [ ] 更新 `tests/test_runtime_server.py`
- [ ] `tests/test_health.py`
- [ ] `tests/test_runtime_ws_client.py`
- [ ] `tests/test_channels_launch.py`（probe + start smoke）
- [ ] `spec/ARCHITECTURE.md` §11 TODO 加 3 项；§12 新增 12.10/12.11/12.12；拓扑图重画
- [ ] `spec/README.md` requirements 树加 `multi-session.md`
- [ ] `uv run pytest tests/ -q` 全绿