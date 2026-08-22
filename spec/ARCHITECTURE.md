# MonoX 架构设计

MonoX 是一个自托管的 agent runtime。本文档是架构的唯一权威说明。

---

## 1. 一句话架构

**系统 = 模块 + 协议。**

- **模块**：有边界、可替换的实现单元。
- **协议**：模块之间的稳定契约，用 `Protocol` + 不可变 `@dataclass(frozen=True)` 表达。
- 模块只通过协议交互，不直接依赖对方的具体实现。替换实现不改协议，就不动其他模块。

---

## 2. 架构总览

MonoX 由 **Runtime 进程** + **N 个 Channel 进程**组成：

- **Runtime 进程**（`python run.py`）：`SessionManager`（多 LoopEngine + idle 销毁
  + checkpoint 恢复）+ `RuntimeServer`（多 session_key ws 索引 + last_active_source
  fan-out）+ `HealthServer`（`GET /health`）；**对 channel 类型一无所知**，只接 ws 帧
- **Channel 进程**（每个 `extensions/channels/<name>/__main__.py` 一个）：拉起一个
  channel adapter（monoDesk ws server / terminal stdio TUI / feishu lark / textual
  TUI）+ 一个 `RuntimeWSClient` 连 Runtime；channel-specific 依赖只在该进程加载

```
┌────────────────── Runtime 进程 ─────────────────────┐
│                                                       │
│   ┌────────────────┐    register/unregister            │
│   │ SessionManager │◄─────────────────┐               │
│   │  dict[sk, SL]  │                  │               │
│   │  + idle sweep  │                  │               │
│   └──┬──────────┬──┘                  │               │
│      │ dispatch_inbound              │               │
│      ▼                                │               │
│   ┌────────────────┐  per-sk output_q │               │
│   │ LoopEngine(sk) │──────────────────►│               │
│   └────────────────┘                  │               │
│                                       │               │
│   ┌────────────────┐   ws (:8765)     │               │
│   │ RuntimeServer  │◄──── channel 进程 │               │
│   │ _clients[(sk,  │     hello.data.  │               │
│   │   src), ws]    │     session_key  │               │
│   │ last_active_   │     + .source    │               │
│   │   source[sk]   │                  │               │
│   └────────────────┘                  │               │
│                                       │               │
│   ┌────────────────┐  http (:8767)    │               │
│   │ HealthServer   │  GET /health →   │               │
│   └────────────────┘  {sessions:[…]}  │               │
└───────────────────────────────────────────────────────┘

   Channel 进程 (×N，每个独立)：

   ┌── monoDesk ──┐   ┌── terminal ──┐   ┌── feishu ──┐   ┌── textual ──┐
   │ RuntimeWSCl. │   │ RuntimeWSCl. │   │ RuntimeWSCl│   │ RuntimeWSCl │
   │ Channel: ws  │   │ Channel: stdio│  │ Channel: ws│   │ Channel: TUI│
   │ :8766        │   │              │   │ lark-oapi  │   │ textual     │
   │ source=      │   │ source=      │   │ source=    │   │ source=     │
   │ "monodesk"   │   │ "terminal"   │   │ "feishu"   │   │ "textual"   │
   └──────────────┘   └──────────────┘   └────────────┘   └─────────────┘
```

**LoopEngine 完全不知道 channel 是什么**。它把 InboundEvent 投到 input_q，从
output_q 拿 StreamEvent；RuntimeServer 把 output_q 翻译成 ws 帧发给对应 (sk, source)
的 channel 进程；channel 进程把帧分发给自己的 adapter。

### 依赖方向（强制）

```
extensions.channels.<name>  ──► core（只依赖协议 + RuntimeWSClient）
extensions.channels._runtime ──► core.channel.base + core.runtime_ws_client
run.py                       ──► core（runtime-only；不装配 channel）
core                         ──► 只依赖 httpx / tomli / websockets，绝不 import extensions
```

`extensions/channels/<name>/__main__.py` 是 channel 进程入口——每个 channel 自带
一个，只依赖 `core`（不依赖其他 channel）。`extensions/channels/_runtime.py` 是
所有 channel 共享的 mini-runtime helper（pump_inbound / pump_outbound /
run_channel），不属于 core。

### 三层的职责

| 层 | 职责 | 是否可换 |
|---|---|---|
| `core/` | 稳定内核：协议 + ReAct 引擎 + 存储/执行抽象。零 UI / 零 IM / 零 LLM SDK | 稳定，不轻易改 |
| `extensions/` | 适配层：channel adapter、skill | 可随意重写 |
| `run.py` | 装配层：实例化具体实现，注入 core | 每用户可改 |

---

## 3. 协议清单

这是 core 对外暴露的全部契约。所有跨模块通信都走它们。

| 协议 | 职责 | 当前实现 | 消费者 |
|---|---|---|---|
| `Channel` | 双向桥接外部信号与 core | `extensions/channels/*` | `gateway` |
| `LLMProxy` | 流式调用 LLM，产出 `LlmChunk` | `core/llm_proxy/OpenAIStreamProxy` | `loop/engine` |
| `Tool` | 可被 agent 调用的工具 | `core/loop/tools/*` | `loop/engine` |
| `CheckpointStore` | 对话历史持久化 + 恢复 | `core/loop/checkpoint.py` | `loop/engine` |
| `MemoryStore` | 长期记忆读写 | `core/memory/fs_store.py` | `loop/engine` |
| `SandboxRunner` | 执行 shell 命令 | `core/sandbox/bash_runner.py` | `loop/tools/bash.py` |

### 鸭子类型（为什么用 Protocol）

协议检查的是**结构**，不是继承关系：只要一个对象有 `run()` 且签名匹配，`isinstance(obj, SandboxRunner)` 就成立，不需要它继承任何基类。

这就是鸭子类型：不看它"是什么类"，只看它"能不能做这件事"。所以 adapter 接入不需要改 core，只需要满足方法签名。

---

## 4. 数据流

### 入站（外部 → agent）

```
外部信号
  → Channel.listen() 产出 InboundEvent
  → Gateway fan-in 进 input_queue
  → EventWrapper.wrap() 包装成 XML 文本
  → 追加进 messages[]
  → LoopEngine 推理
```

### 出站（agent → 外部）

```
LoopEngine 产出 StreamEvent（TokenChunk / ToolEnd / FinalMessage / ...）
  → Gateway fan-out
  → EventWrapper.parse_output() 解析 <send channel="xxx">
  → 路由到对应 Channel.send()
```

入站统一包装，出站显式路由。无 `<send>` 标签时回 `pending_channel`。

---

## 5. 数据结构契约

当前集中定义在 `core/protocol/events.py`，按域分组如下。量还小，单文件单一 import 源；等协议域变多再拆。

| 域 | 数据结构 | 方向 |
|---|---|---|
| 入站 | `InboundEvent` | Gateway → Loop |
| 出站流 | `TokenChunk` `ReasoningChunk` `ToolStart` `ToolEnd` `StatusChange` `MetricChunk` `FinalMessage` `Card` `ErrorEvent` | Loop → Gateway |
| 工具 | `ToolCall` `ToolResult` | Loop ↔ Sandbox |
| LLM | `LlmChunk` | LLMProxy → Loop |
| Checkpoint | `CheckpointRecord` | Loop → CheckpointStore |
| Sandbox | `SandboxResult` | SandboxRunner → BashTool |

全部 `@dataclass(frozen=True)`，单向数据流，错误用 `status` 字段表达。

---

## 6. core 子模块

core 内部同样按「子模块 + 协议」组织。

| 子模块 | 职责 | 协议角色 |
|---|---|---|
| `protocol/` | 定义全部协议 + 数据结构 | 协议持有者 |
| `protocol/wire_frames.py` | Runtime↔Gateway ws 帧格式 + 编解码 | 协议持有者 |
| `channel/base.py` | 定义 `Channel` 协议 | 协议定义 |
| `event_wrapper.py` | 入站 XML 包装 / 出站 `<send>` 路由 | 纯函数，无协议 |
| `runtime_server.py` | Runtime 端 ws server（`RuntimeServer`） | 消费 `InboundEvent` / `StreamEvent` |
| `loop/` | ReAct 主循环 + 工具注册 | 消费 `LLMProxy`/`Tool`/`CheckpointStore`/`MemoryStore` |
| `llm_proxy/` | OpenAI-compatible 流式调用 | 实现 `LLMProxy` |
| `sandbox/` | bash 执行后端 | 实现 `SandboxRunner` |
| `memory/` | 记忆存储 | 实现 `MemoryStore` |
| `config.py` | 统一 `config.toml` 加载（含 `[server]` 段） | 无协议 |

---

## 7. LoopEngine 细化

`LoopEngine` 是 core 的调度核心，不依赖任何具体实现，只依赖协议。

```
InboundEvent
     │
     ▼
┌──────────────────────────────────────┐
│            LoopEngine.run            │
│  while True:                         │
│    ev = await input_queue.get()      │
│    final = await _react(...)         │
│    output_queue.put(FinalMessage)    │
└──────────────┬───────────────────────┘
               ▼
        _react（ReAct 循环）
```

### ReAct 状态机

```
              ┌────────────► thinking ─────────────┐
              │                                    │
       有 tool_calls                        无 tool_calls
              │                             且无新事件
              ▼                                    │
          tooling ──工具执行完──► thinking          ▼
              │                                wait_io
              │ wait_io 工具                      │
              └────────► wait_io ◄──新 InboundEvent─┘
```

每一步：

1. drain `input_queue`，聚合新用户消息
2. `assemble_messages`（system + memory + skill summary + messages）
3. `LLMProxy.stream()` 流式产出文本 / tool_calls / reasoning
4. 无 tool_calls 且无新事件 → 写 checkpoint，进 `wait_io`
5. 有 tool_calls → 逐个 `Tool.execute()` → 写 checkpoint → 回到 thinking

### wait_io 设计

`wait_io` 是一个内置 tool，含义是「agent 主动结束当前 turn，等待外部输入」。

- agent 调用 `wait_io` 时，engine **不真正执行**它，直接标记 `has_wait_io`
- 当前 step 结束后进入 `wait_io` 状态，`_react` 返回
- 新 `InboundEvent` 到达时，`LoopEngine.run` 再次进入 `_react` 继续

这样 agent 可以自己控制「一个 turn 该停在哪」，而不是被 max_steps 或空输入推着走。

---

## 8. 关键存储设计

### CheckpointStore：对话历史持久化

- 职责：把 `messages` + `step_idx` 存下来，重启后恢复，让对话不丢。
- 当前实现 `JsonlCheckpointStore`：每步追加一条 JSONL，`load_latest` 读最后一条。
- 未来可换 sqlite，协议不变。

### MemoryStore：长期记忆

- 职责：agent 的长期记忆，跨 session 沉淀。
- 协议只定义 `read_index / write_note / update_index`。
- 当前实现 `FsMemoryStore` 用文件系统（`Memory.md` 索引 + `notes/` 目录）。
- **文件只是实现之一**，未来可换向量库等后端。

### SandboxRunner：命令执行

- 职责：执行 shell 命令，返回 `SandboxResult(stdout, stderr, exit_code)`。
- 当前实现 `BashRunner` 用 `asyncio.create_subprocess_shell`。
- 未来可换 docker exec / ssh，协议不变。

---

## 9. extensions 细化

| 子模块 | 职责 | 协议角色 |
|---|---|---|
| `channels/<name>/` | 每个 channel 一个包（`monodesk` / `terminal` / `feishu` / `textual_chat`），含 `__main__.py` + `__init__.py` | 实现 `Channel`，由 `__main__.py` 起独立进程 |
| `channels/_runtime.py` | 共享 mini-runtime helper（`run_channel(channel, ws_client)` 起 channel + 双 pump gather） | 私有 helper，不属于 core |

新增 channel：在 `extensions/channels/<name>/` 加包 + 实现 `Channel`，启动用
`uv run python -m extensions.channels.<name> --runtime-url=...`。`extensions/channels/_runtime.py`
的 `run_channel` 自动串好 ws pump。
新增 skill：放 `extensions/skills/<name>/SKILL.md`。

---

## 10. 边界规则

1. core 不 import extensions。
2. core 只依赖 `httpx` + `tomli`。
3. 跨模块事件 `@dataclass(frozen=True)`，单向数据流。
4. 接口用 `Protocol` + `runtime_checkable`，不强制继承。
5. 新增协议属于 core 稳定 API 变更，先确认不是 adapter 该做的事。
6. 错误用返回 `status` 字段，不用异常控制流。

---

## 11. 未实现（按依赖顺序）

| 项 | 状态 | 文档 |
|---|---|---|
| L1/L2/L3 上下文压缩 | L1 函数已就位，engine 未调用 | `requirements/context-compression.md` |
| 干净退出 shutdown_event | 未实现 | `requirements/shutdown.md` |
| LLM harness（retry/fallback/rate-limit） | 未实现 | `requirements/llm-harness.md` |
| Memory 自动摘要/维护 | 未实现 | `requirements/context-compression.md` |
| bash 危险命令 HITL hook | 未实现 | `goal.md` 中期目标 |
| session → channel 路由（基于 session_key） | Runtime 端 last_active_source 单 conn 路由 | 第 12 节 |
| Runtime 多 session 化（lazy create + idle destroy + checkpoint 恢复） | 已实现 | `requirements/multi-session.md` + 第 12 节 |
| Channel 独立进程化（每个 channel 一个 `__main__`） | 已实现 | 第 12 节 |
| HTTP `/health` 端点（列活跃 sessions） | 已实现 | 第 12 节 |

---

## 12. Runtime + Channel in-process + 配置驱动启动

> 本节是 Runtime 单进程内启动 SessionManager + RuntimeServer + HealthServer +
> 配置驱动的 in-process channel adapter 的完整说明。Runtime 不感知 channel 协议。
> MonoDesk desktop 客户端 spec 在另一个仓库，但 wire 帧字段必须一字不差。

### 12.1 为什么这样设计

单 Runtime 进程内 in-process 拉 channel adapter 有三个考虑：

1. **运维简单** —— 一个 `run.py` 脚本按 `config.toml` 的 `[[channels]]` 列表拉起
   所有 channel 监听（terminal TUI / monoDesk ws server / feishu lark / textual
   TUI）。不需要分别起 N 个进程。
2. **多 session 化** —— monoDesk 一个用户开多个会话（不同 session_id）、飞书不同
   群（不同 chat_id）天然是独立 session_key；Runtime 用 SessionManager 持有
   `dict[session_key → SessionLoop]`，lazy create + idle destroy + checkpoint 恢复。
3. **channel 不参与 routing 决策** —— 所有 fan-out 都在 Runtime 内按
   `last_active_source` 单 conn 路由；channel 只通过 ws 协议跟 Runtime 通信。

### 12.2 进程拓扑

```
┌─────────────────────────── Runtime 进程（单进程） ─────────────────────┐
│                                                                       │
│   ┌──────────────────┐  register/unregister                            │
│   │ SessionManager   │◄─────────────────┐                             │
│   │  dict[sk, SL]    │                  │                             │
│   │  + idle sweep    │                  │                             │
│   └──┬─────────────┬─┘                  │                             │
│      │ dispatch_inbound                 │                             │
│      ▼                                  │                             │
│   ┌──────────────────┐  per-sk output_q │                             │
│   │ LoopEngine(sk)   │─────────────────►│                             │
│   └──────────────────┘                  │                             │
│                                          │                             │
│   ┌──────────────────┐   ws (:8765)     │                             │
│   │ RuntimeServer    │◄──── in-process  │                             │
│   │ _clients[(sk,    │     channels     │                             │
│   │   src), ws]      │      + 外部工具   │                             │
│   │ last_active_     │                  │                             │
│   │   source[sk]     │                  │                             │
│   └──────────────────┘                  │                             │
│                                          │                             │
│   ┌──────────────────┐  http (:8767)    │                             │
│   │ HealthServer     │  GET /health     │                             │
│   └──────────────────┘                  │                             │
│                                          │                             │
│   ┌──────────────────────────────────────────────────────┐           │
│   │ in-process channel adapters (按 config 启动)            │           │
│   │                                                       │           │
│   │  TerminalChannel  ─► stdio TUI                        │           │
│   │  MonoDeskChannel  ─► ws server :8766 (给桌面连)       │           │
│   │  FeishuChannel    ─► lark-oapi WS + HTTP              │           │
│   │  TextualChannel   ─► textual TUI                      │           │
│   │                                                       │           │
│   │  每个 channel 自带一个 RuntimeWSClient 连 :8765       │           │
│   │  通过 ws 跟 RuntimeServer 通信（in-process ws）        │           │
│   └──────────────────────────────────────────────────────┘           │
└───────────────────────────────────────────────────────────────────────┘
```

### 12.3 ws 帧格式 v1

帧信封：`{"v": 1, "type": <one_of_13>, "seq": <int>, "ts": <float>, "data": <object>}`

13 个 `type` 字符串集中定义在 `core/protocol/wire_frames.py:FrameType`。

#### 出站（Runtime → Channel ws client）

| `type` | 触发事件 | `data` schema |
|---|---|---|
| `status` | `StatusChange` | `{state}` |
| `token` | `TokenChunk` | `{text}` |
| `reasoning` | `ReasoningChunk` | `{text}` |
| `tool_start` | `ToolStart` | `{name, args}` |
| `tool_end` | `ToolEnd` | `{name, latency_ms, result}` |
| `metric` | `MetricChunk` | `{metrics}` |
| `final` | `FinalMessage` | `{text, metrics}` |
| `card` | `Card` | `{data}` |
| `error` | `ErrorEvent` | `{code, msg, retryable}` |

> Runtime↔Channel 间**不**发 `hello` 帧（hello 是 monoDesk adapter 给 desktop 用的）；
> RuntimeWSClient 在连接建立后内联构造 hello dict 直发，hello 在 wire 上只用于
> Runtime 注册 `(sk, source)`。

#### 入站（Channel → Runtime）

| `type` | 产出 `InboundEvent` | session_key 规则 |
|---|---|---|
| `user_input` | `kind="message"` | `data.session_key` 优先，缺失回 hello 注册的 session |
| `command` | `kind="command"` | **强制** hello 注册的 session（防跨 session 误触） |
| `interrupt` | `kind="interrupt"` | **强制** hello 注册的 session |

### 12.4 seq 单调生成

- **Runtime 端**用 `itertools.count()` 单调生成 `seq`，写到每个出站帧
- **channel 进程内 MonoDesk adapter** 用 `send_frame_direct(seq, frame)` 把 Runtime seq
  透传给 desktop 客户端——**不重写 seq**
- 桌面客户端看到的 `seq` 来自 Runtime 单一来源，绝对单调

### 12.5 Routing 归属（Runtime 端 last_active_source）

Routing **完全在 Runtime 进程内**。channel 不参与 routing 决策。

```
Runtime fan-in:
  ws recv → InboundEvent(session_key, source)
    → RuntimeServer._last_active_source[sk] = source
    → SessionManager.dispatch_inbound(ev)
        新 sk → lazy create SessionLoop（从 JsonlCheckpointStore 恢复）
        已有 sk → 直接 put 到 input_q

Runtime fan-out:
  SessionLoop.output_q → RuntimeServer per-session consumer task
    → 按 last_active_source[sk] 找到对应 ws conn
    → 该 conn 收 frame；其他 conn（同 sk 不同 src）不收
    → conn 不存在 → 本帧丢弃
```

**Routing 策略（单 conn fan-out）**：每个 `session_key` 只发给 `last_active_source`
对应的那一个 ws conn。多 channel 共用 `session_key`（如 terminal + monodesk 都用
"default"）时，**只有最近上行过的那个 channel 收得到下行**——monoDesk 提问 → 回答
只在 monoDesk 终端出现，terminal 不出现。其他 channel 通过新 `session_key` 隔离。

**conn 断开清理**：`last_active_source[sk]` 指向的 conn 断开时，若该 source 没有
其他 conn 注册 → 清 entry；下次 `dispatch_inbound` 重新 set。

### 12.6 多 session + idle 销毁

SessionManager 是 Runtime 端多 session 化的核心：

```
SessionManager
├── dispatch_inbound(ev):  新 sk → _create；已有 → put；更新 last_active_ts
├── _create(sk):           构造 SessionLoop + JsonlCheckpointStore(sk专属)
│                           + 注册到 RuntimeServer.per_session_output_consumer
├── _idle_sweeper():       每 N 秒扫描，超 idle_timeout 的 destroy + unregister
└── active_sessions():     返回当前活跃 sk 列表（给 HealthServer 用）
```

**lazy create**：新 `session_key` 第一次收到 inbound 才构造 SessionLoop + 启动
loop task。

**idle destroy**：`now - last_active_ts > IDLE_TIMEOUT`（默认 300s）→ 取消 loop
task + 注销 per-session consumer + 从 `_sessions` 删除。

**恢复**：destroy 后新 inbound 到达 → 重新 `_create` → JsonlCheckpointStore 从
`<memory_root>/<sk>/checkpoint.jsonl` 读历史 → LoopEngine `_restore` 重建上下文。

**per-session 隔离**：每个 `sk` 一份独立 `JsonlCheckpointStore`（多 session 不能
共享 jsonl 文件）；`FsMemoryStore` / LLM / ToolRegistry / Compression 共享（无
session 状态）。

### 12.7 HTTP `/health` 端点

Runtime 进程在 `:8767` 暴露 `GET /health`：

```bash
$ curl http://127.0.0.1:8767/health
{"sessions": ["default", "chat-42"]}
```

stdlib `asyncio.start_server` + 手写 HTTP/1.1 request line 解析，无第三方依赖。
`session_provider` 回调指向 `SessionManager.active_sessions()` ——不缓存，每次
请求拿新值。

### 12.8 启动方式（单脚本）

```bash
# 终端 1：启动 Runtime（核心进程 + 配置驱动 channels）
uv run python run.py [config.toml]
#   --server-host 127.0.0.1  (默认)
#   --server-port 8765       (默认)
#   --health-port 8767       (默认)
#   --idle-timeout 300       (默认秒)
#   --no-channels            (只起 Runtime + ws server，不拉 channel)

# config.toml 写法：
#   [[channels]]
#   kind = "terminal"
#
#   [[channels]]
#   kind = "monodesk"
#   [channels.options]
#   port = 8766
#
#   [[channels]]
#   kind = "feishu"
#   [channels.options]
#   app_id = "cli_xxx"
#   app_secret = "xxx"
#
#   [[channels]]
#   kind = "textual_chat"
#   [channels.options]
#   debug = false

# 无 [[channels]] 配置时，run.py 默认起一个 terminal channel

# 任意时刻查 Runtime 状态：
curl http://127.0.0.1:8767/health
```

**高级用法**：把单个 channel 跑在独立进程（如调试某个 channel、不影响其他
channel 时）。每个 channel 都有自己的 `__main__.py`：

```bash
uv run python -m extensions.channels.monodesk \
    --runtime-url=ws://127.0.0.1:8765 \
    --session-key=default
# 但此时 Runtime 那边必须不拉同 kind 的 channel，否则 last_active_source 冲突
```

### 12.9 Channel 故障恢复

- **Channel ws 断线自动重连**：`RuntimeWSClient` 用指数退避 (0.5/1/2/4/8/16s) 无限
  重试。Runtime 重启不会拖死 channel；channel 内部崩溃 Runtime 会从 `_clients` 移除。
- **Runtime 同 `(sk, source)` 替换**：同 channel 第二次连 → Runtime 主动
  close(1011) 旧连接；不同 source 不替换（允许多 channel 共 sk 但 fan-out 单 conn）。
- **慢客户端隔离**：per-session consumer task send 抛异常 → 该 conn 从 `_clients`
  移除 → 后续帧不再 send 给它。
- **死连接不阻塞**：Runtime 推帧时 send 抛异常 → 该 conn 本帧丢弃 + cleanup。

### 12.10 SessionManager ↔ RuntimeServer 解耦

`SessionManager` 不直接 import `RuntimeServer`；通过两个 async 回调协作：

- `outbound_register(sk, output_q)`：create SessionLoop 后调用，让 RuntimeServer
  启动该 sk 的 per-session outbound consumer task
- `outbound_unregister(sk)`：destroy SessionLoop 时调用，让 RuntimeServer 取消
  consumer + 清 last_active_source

`run.py` 装配时提供这两个回调（封装 `server.register_outbound_queue` /
`server.unregister_outbound_queue`）。RuntimeServer 单测可独立运行（注入 mock
handler + mock output_q），SessionManager 单测同理。

### 12.11 Channel 配置 schema

```python
@dataclass(frozen=True)
class ChannelConfig:
    """config.toml [[channels]] 一项。"""
    kind: str               # "terminal" / "monodesk" / "feishu" / "textual_chat"
    options: dict[str, Any] # 各 channel 自解析（host/port/credentials/...）
    channel_raw: dict       # 原始 channel dict（含 [channels.feishu] 等子表）

@dataclass(frozen=True)
class MultiChannelConfig:
    channels: list[ChannelConfig]
    default_channel: str
```

`run.py` 按 `kind` 用 `_build_channel(kind, options, session_key)` 派发构造；
未识别的 kind 警告并跳过。每个 channel 自己解析 `options`（channel-specific
schema 不进 core）。
