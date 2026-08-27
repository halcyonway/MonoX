# async-task: 异步任务（subagent 是其中一个经典场景）

## 背景

MonoX 当前 `LoopEngine` 是**单 turn 串行**模型：用户来一条消息 → ReAct 跑到 `wait_io` → 等下一条。这是同步交互的正确抽象，但缺一类关键场景：

> agent 在 turn 中遇到「需要并行做 / 需要花很久做」的事情时，它想继续当前 turn，但又想知道那件事什么时候做完、能不能看到中间过程、必要时主动中止。

典型场景：
- 后台跑 `bash` 跑 20 分钟（前端打包、长链路测试）
- 让另一个 agent 「帮我审一下这个 PR」「帮我搜全网竞品」
- 跨多 repo 改代码（开 N 个 subagent 各改一块）

当前只能让 agent 把所有事串行做完（一个 20 分钟 turn，UI 卡住、不能 cancel、不能并行）。本需求引入**统一的「异步任务」抽象**，subagent 是其中最经典的实现方式。

## 目标

1. **统一抽象**：引入「异步任务（AsyncTask）」为第一类概念；subagent / 长命令 / 任何「agent 主动 fire-and-forget 后台工作」都走同一套机制。
2. **最小改动**：复用现有 `SessionLoop` + `JsonlCheckpointStore` + `Tool` 协议 + `wait_io` / interrupt 机制；不为新概念造新内核。
3. **可中断**：agent 主动 `cancel_task`、MonoDesk 手动按按钮、timeout 兜底——三种中断路径统一到一个 cancel 入口。
4. **多 channel 一致**：subagent 状态 / 输出 / 取消事件对 MonoDesk / terminal / feishu / textual 表现一致；非 UI 通道用文本 fallback。
5. **可观测**：MonoDesk 有专门的 Tasks 面板，列出 / 详情 / 中断 / 跨链接到 Chat 中的 fork 调用。

## 边界

### 改

- **新增 core 模块** `core/async_task.py`（`AsyncTask` dataclass + `AsyncTaskManager`）。不引新依赖。
- **新增 3 个内置 tool**：`core/loop/tools/{fork_task,poll_task,cancel_task}.py`。注册到 `run.py` 的 `ToolRegistry`。
- **扩展 wire 协议**：在 `core/protocol/wire_frames.py` 加 7 个 frame type（5 outbound + 2 inbound）。`FrameType` 集中定义。
- **扩展 `RuntimeServer`**：新增 `async_task_event` 全局 fan-out 路径（按 task_id 路由到所有 MonoDesk 客户端）；新增 2 个 inbound handler。
- **MonoDesk**：新增 `Tasks` 侧栏页面（紧跟 Chat / Skills 之后）。

### 不改

- **不改 `LoopEngine`**。AsyncTask 是 SessionLoop 的特殊用法，不动 engine 本身的 ReAct 状态机。
- **不改 `SessionManager` 的 idle 销毁逻辑对 async task 应用**：async task 有自己的 deadline，不参与普通 session 的 idle sweep。
- **不改 `wait_io` 语义**。AsyncTask 完成时通过 `AsyncTaskManager.notify_parent` 投 system event 到父 session 的 input_q，触发父 engine 已有的「新 InboundEvent 唤醒」路径（`LoopEngine.run` 第 141-175 行的 `await sub_queue.get()`）。
- **不改 `core/protocol/events.py` 现有 dataclass 字段**。新增 2 个 outbound dataclass（`AsyncTaskEvent` / `AsyncTaskStatus` / `AsyncTaskCreated` / `AsyncTaskList` / `AsyncTaskSnapshot`）。
- **不改 sandbox / LLM proxy / memory**。

### 遵循 Core 最小改动原则

- core 不为「让 MonoDesk 好看」做妥协。`AsyncTaskManager` 不持有 UI 概念；任务列表查询是普通 HTTP/WS API，跟 `/health` 同层。
- channel-specific 的 UI 行为全部放在 `MonoDesk` 和 channel adapter 里。

---

## 概念

### AsyncTask（异步任务）

```python
@dataclass
class AsyncTask:
    task_id: str                # 32-hex, eg "t_4f9e..." 由 AsyncTaskManager 生成
    kind: str                   # "subagent" / "bash_long" / ... (本期只实现 subagent)
    description: str            # 第一句任务描述（UI / poll 用）
    meta: dict[str, Any]        # 扩展 kv，agent fork 时填
    parent_session_key: str     # 谁 fork 的（主 session_key）
    child_session_key: str      # "async:" + task_id（复用 SessionLoop）
    status: Literal["pending","running","completed","failed","cancelled","timed_out"]
    created_at: float
    started_at: float | None    # SessionLoop.start 实际启动时间
    finished_at: float | None
    timeout_sec: float          # 默认 1800（30min）
    final_text: str | None      # 子 agent 最后一句 final（completed 时填）
    error: str | None           # 失败原因
    cancel_reason: str | None   # agent / user / timeout
```

### AsyncTaskManager

Runtime 进程内的单例（被 `run.py` 持有，跟 `SessionManager` 同层）。

职责：
- start(description, parent_sk, meta, timeout_sec, kind) → task_id（**同步**返回；内部 async 启动 SessionLoop）
- cancel(task_id, reason) → ok / not_found
- list(parent_sk=None, status=None) → list[AsyncTaskSummary]
- get(task_id) → AsyncTaskSnapshot（含最近 N 条 event）
- 把子 SessionLoop 的 output_q 桥接到 wire（async_task_event 帧）
- timeout 调度：每个 task 起一个 `asyncio.TimerHandle` 在 `started_at + timeout_sec` 触发自动 cancel

### Subagent 的 child SessionLoop 是什么样

- `session_key = "async:" + task_id`
- `messages` 从空起步
- 第一条 user message：`{"role": "user", "content": user_input_event_xml(<InboundEvent(kind="message", text=description, source="async_task", event_type="async-task-prompt", meta={...})>)}`
- system prompt 跟普通 SessionLoop 一样（共享，不做 subagent 专用 prompt）
- 复用 `JsonlCheckpointStore` 落到 `<state_root>/async/<task_id>/checkpoint.jsonl`——独立目录
- 工具集**完全继承**当前 ToolRegistry（agent 能 fork 时调的所有 tool，subagent 也能调；包括 fork_task 本身——**支持 subagent 嵌套**）
- 不注册到 `RuntimeServer._outbound_qs`（不走 channel fan-out），由 `AsyncTaskManager` 自己消费 output_q

---

## 架构总览

```
                        ┌──────────────────────────────────────────────────┐
                        │            Runtime 进程（单进程）                    │
                        │                                                  │
   parent_sk ──►        │   ┌──────────────────┐                           │
   SessionLoop ─────────┼─► │ AsyncTaskManager │ ◄── timeout scheduler     │
                        │   │                  │                            │
   fork_task tool ─────►│   │  dict[task_id,   │   per-task subscriber      │
                        │   │        AsyncTask]│─────► AsyncTaskWireBridge  │
   cancel_task tool ───►│   │                  │                            │
                        │   └────────┬─────────┘                            │
   poll_task tool ─────►│            │  start / cancel / list / get         │
                        │            ▼                                     │
                        │   ┌──────────────────┐                            │
                        │   │ SessionManager   │                            │
                        │   │  dict[sk → SL]   │                            │
                        │   │   │              │                            │
                        │   │   ├── "default"  ◄──── normal session        │
                        │   │   ├── "chat-42"  ◄──── normal session        │
                        │   │   └── "async:t_4f9e..." ◄── child session    │
                        │   └──────────────────┘                            │
                        │            │                                     │
                        │            ▼                                     │
                        │   ┌──────────────────┐                            │
                        │   │ RuntimeServer    │ ◄── async_task_event fanout│
                        │   │  :8765 ws        │     到所有 MonoDesk 客户端  │
                        │   └──────────────────┘                            │
                        └──────────────────────────────────────────────────┘
                                         │
                       ┌─────────────────┼─────────────────┐
                       ▼                 ▼                  ▼
                 MonoDesk (rich UI)   terminal (text)    feishu (text)
                 - Tasks 侧栏         - 卡片 fallback    - 卡片 fallback
                 - 详情 + cancel       - 不可 cancel       - 不可 cancel
                 - 跨链接到 Chat
```

---

## 数据流

### Fork（agent 主动开任务）

```
主 agent turn 中调 fork_task(description="...", meta={...}, timeout_sec=1800)
       │
       │  (Tool.execute 同步路径)
       ▼
AsyncTaskManager.start()
       │
       │  1) 生成 task_id = "t_" + uuid4().hex[:12]
       │  2) 校验 timeout（min=10, max=7200, default=1800）
       │  3) 构造 child_sk = "async:" + task_id
       │  4) SessionManager.dispatch_inbound(
       │       InboundEvent(
       │         session_key=child_sk, kind="message",
       │         text=description,
       │         source="async_task",
       │         event_type="async-task-prompt",
       │         meta={"parent_sk": parent_sk, "task_id": task_id, "kind": "subagent"},
       │       )
       │     )
       │     → lazy create SessionLoop with child_sk
       │     → put event 到 input_q
       │     → SessionLoop.start() → 立即起 ReAct loop
       │
       │  5) 启动 AsyncTaskBridge consumer task：
       │       async for ev in child.output_q: emit AsyncTaskEvent(task_id, ev)
       │
       │  6) schedule timeout: handle = loop.call_later(timeout_sec, cancel, task_id, reason="timeout")
       │
       │  7) AsyncTaskManager 内部记录 AsyncTask(task_id, ..., status="running")
       │
       │  8) 发 AsyncTaskCreated 到父 session 的 output_q（让父对话流里出现一张 "task forked" 卡片）
       │     + 发 async_task_created wire 帧到所有 MonoDesk（让 Tasks 面板立即多一行）
       │
       │  9) fork_task tool 返回 ToolResult:
       │       status="ok", stdout='{"task_id":"t_4f9e...", "status":"running", "timeout_sec":1800}'
       │
       ▼
主 agent turn 继续（不等子 agent）
```

### Subagent 执行 → 父 agent 感知

```
child SessionLoop 跑 ReAct：
  - LLM stream → output_q.put(TokenChunk(...))
       │
       │  AsyncTaskBridge 消费
       ▼
AsyncTaskEvent(task_id, payload=TokenChunk(...), ts=...)
       │
       ├──► RuntimeServer.async_task_event_broadcast(task_id, AsyncTaskEvent)
       │       │
       │       │  对所有 ws conn 发送 {"type":"async_task_event", "data":{task_id, payload}}
       │       ▼
       │    MonoDesk Tasks 详情页累积 token
       │    其他 channel 收到后 channel adapter 决定渲染（见 §10）
       │
       └──► AsyncTaskManager 维护 per-task ring buffer（最近 N 条事件）
              用于 poll_task(get task) 时不带 ws 也能看到摘要
```

### 完成通知父 agent（关键的「唤醒」机制）

```
child SessionLoop 触发 FinalMessage：
  AsyncTaskBridge 收到 FinalMessage 时：
       │
       ├──► AsyncTaskManager.mark_done(task_id, final_text=ev.text)
       │       - status = "completed"
       │       - finished_at = now
       │       - final_text = ev.text
       │       - cancel timeout handle
       │       - 写 task.json
       │
       └──► AsyncTaskManager.notify_parent(task_id, parent_sk, summary)
                │
                │  构造 InboundEvent:
                │    session_key=parent_sk
                │    kind="message"
                │    text=(
                │      f'<event type="async-task-result" source="async_task" '
                │      f'task_id="{task_id}" status="completed">\n'
                │      f'  <text>{summary}</text>\n'
                │      f'</event>'
                │    )
                │    event_type="async-task-result"
                │    meta={"task_id": task_id, "status": "completed"}
                │
                ▼
             SessionManager.dispatch_inbound(ev)
                - 找到 parent_sk 的 SessionLoop（不存在时 lazy create + 复活 checkpoint）
                - put 到 input_q
                - 若父 engine 在 wait_io：LoopEngine.run 第 141 行 `sub_queue.get()` 拿到 → 进入 react → 把 XML 解析为 user message → LLM 看到结果
```

这就是复用了 wait_io 的「新事件唤醒」机制，不改 engine 一行代码。

### Cancel / Timeout / 失败（三种入口同一出口）

```
cancel_task(task_id, reason="agent")       ← agent 主动
       │
async_task_cancel inbound 帧             ← MonoDesk 手动
       │
timeout handle 触发                       ← 30min 兜底
       │
       ▼
AsyncTaskManager.cancel(task_id, reason)
       │
       │  1) 找到 child_sk 对应的 SessionLoop.task
       │  2) task.cancel() → 抛 CancelledError → 引擎清理（_messages/_step_idx 回滚，output_status=idle）
       │  3) mark_done(task_id, status="cancelled"/"timed_out", cancel_reason=reason)
       │  4) notify_parent(...)：投 system-notify 事件到父 input_q，让父 agent 知道「task 已取消」
       │  5) 发 async_task_status wire 帧到所有 MonoDesk
       │  6) bridge task 自然结束（output_q 关闭）
```

### Poll（agent 主动查状态）

```
poll_task(task_ids=["t_4f9e", "t_..."])
       │
       ▼
AsyncTaskManager.list(task_ids, ...)
       │
       │  对每个 task_id：
       │    - 读 task.json → AsyncTaskSummary
       │    - 读 events ring buffer 最近 K 条 → 摘要
       ▼
返回 ToolResult(stdout=json.dumps(summaries, ...))

agent 想拿完整输出：用 poll_task 后再决定要不要 cancel 或者 fork 新的 subagent 处理结果
```

---

## 协议：wire frame 扩展

> 全部集中在 `core/protocol/wire_frames.py:FrameType`，保持「13 + 7 = 20 个 type 集中定义」的现有约定。

### Outbound 新增（5 个）

```python
# AsyncTaskCreated — fork 成功后立即发，让 MonoDesk Tasks 面板出现新行
# data.session_key = parent_sk（沿用现有约定）
{
  "v": 1, "type": "async_task_created",
  "seq": ..., "ts": ...,
  "data": {
    "session_key": "<parent_sk>",
    "task_id": "t_4f9e...",
    "kind": "subagent",
    "description": "...",
    "meta": {...},
    "parent_session_key": "<parent_sk>",
    "timeout_sec": 1800,
    "created_at": 1732000000.0
  }
}

# AsyncTaskEvent — child SessionLoop 的 StreamEvent 转发
{
  "v": 1, "type": "async_task_event",
  "seq": ..., "ts": ...,
  "data": {
    "session_key": "<parent_sk>",
    "task_id": "t_4f9e...",
    "event": { ... 完整 StreamEvent payload（status/token/reasoning/tool_pending/tool_start/tool_end/metric/final/error/card）... }
  }
}

# AsyncTaskStatus — 状态迁移
{
  "v": 1, "type": "async_task_status",
  "seq": ..., "ts": ...,
  "data": {
    "session_key": "<parent_sk>",
    "task_id": "t_4f9e...",
    "status": "completed" | "failed" | "cancelled" | "timed_out",
    "finished_at": 1732000123.0,
    "duration_sec": 123.0,
    "final_text": "..." | null,
    "error": "..." | null,
    "cancel_reason": "agent" | "user" | "timeout" | null
  }
}

# AsyncTaskList — 列表查询响应
{
  "v": 1, "type": "async_task_list",
  "seq": ..., "ts": ...,
  "data": {
    "session_key": "<requesting_sk>",
    "tasks": [
      {"task_id":"t_...", "kind":"subagent", "description":"...", "status":"running",
       "parent_session_key":"...", "created_at":..., "started_at":..., "timeout_sec":1800, ...},
      ...
    ]
  }
}

# AsyncTaskSnapshot — 详情查询响应（含最近事件摘要）
{
  "v": 1, "type": "async_task_snapshot",
  "seq": ..., "ts": ...,
  "data": {
    "session_key": "<requesting_sk>",
    "task": {... AsyncTaskSummary ...},
    "recent_events": [
      {"kind":"status", "state":"thinking", "ts":...},
      {"kind":"token", "text":"...", "ts":...},
      {"kind":"tool_end", "name":"bash", "latency_ms":214, "result":{...}, "ts":...},
      ...
    ]
  }
}
```

### Inbound 新增（2 个）

```python
# async_task_cancel — MonoDesk 手动中断
{
  "v": 1, "type": "async_task_cancel",
  "data": {"task_id": "t_4f9e...", "reason": "user"}
}

# async_task_list_query — MonoDesk 列表刷新（也用于打开 Tasks 页时拉一次）
{
  "v": 1, "type": "async_task_list_query",
  "data": {"session_key": "<requesting_sk>", "filter": {"status": ["running","completed"]} | null}
}
```

### 向后兼容

- 现有 13 个 frame type 一字不动。
- 新增 7 个 type 编号到 FrameType 集中常量；client 收到未知 type 时 try/catch 忽略（既有约定）。
- AsyncTaskEvent.data.event 字段是完整的 StreamEvent dict，复用 frame_to_stream_event 反序列化逻辑。

---

## 协议：core dataclass 新增

> 加在 `core/protocol/events.py`，保持 `StreamEvent` Union 不变（这些是 wire 层专用，不进 StreamEvent——因为它们不是 loop 产生的）：
> AsyncTaskCreated / AsyncTaskEvent / AsyncTaskStatus / AsyncTaskList / AsyncTaskSnapshot 不属于 StreamEvent，只在 wire 层存在，由 `to_frame` / `from_frame` 直接处理（已有同类先例：`hello_frame` 不进 StreamEvent）。

### 内置 Tool 新增 dataclass（仅 meta 字段需要 dataclass 时）

`AsyncTask` dataclass 本身在 `core/async_task.py`（跟 manager 同文件）。

---

## AsyncTaskManager 接口

> 文件：`core/async_task.py`
> 单例（构造一次，跟 `SessionManager` 同生命周期）。

```python
DEFAULT_TIMEOUT_SEC = 1800.0     # 30 min
MIN_TIMEOUT_SEC = 10.0
MAX_TIMEOUT_SEC = 7200.0         # 2h hard cap（防 agent 死循环占用资源）
EVENT_BUFFER_SIZE = 100          # per-task ring buffer

class AsyncTaskManager:
    def __init__(
        self,
        *,
        session_manager: SessionManager,
        bridge_factory: Callable[[AsyncTask], Awaitable[AsyncTaskBridge]],
        state_root: Path,                  # <state_root>/async/ 落 task.json
        on_event: Callable[[AsyncTaskEvent|AsyncTaskCreated|AsyncTaskStatus], Awaitable[None]],
        time_fn: Callable[[], float] = time.time,
        default_timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ): ...

    async def start(
        self,
        *,
        description: str,
        parent_session_key: str,
        meta: dict[str, Any],
        kind: str = "subagent",
        timeout_sec: float | None = None,
    ) -> AsyncTask:
        """同步返回 AsyncTask（task_id 已生成；child SessionLoop 异步启动）。"""

    async def cancel(self, task_id: str, *, reason: str) -> bool:
        """cancel SessionLoop task + 标记 status='cancelled' + notify_parent。"""

    def get(self, task_id: str) -> AsyncTask | None: ...
    def list(self, *, parent_session_key: str | None = None,
             status: list[str] | None = None) -> list[AsyncTask]: ...
    def snapshot(self, task_id: str) -> tuple[AsyncTask, list[dict]] | None:
        """详情 + 最近 N 条 event。"""

    async def shutdown(self) -> None:
        """Runtime 退出时 cancel 所有 running task，flush state。"""

    # 内部：
    async def _on_child_event(self, task_id: str, ev: StreamEvent) -> None:
        """bridge 调用：推 event 到 ring buffer + 构造 AsyncTaskEvent 调 on_event。"""
    async def _on_child_done(self, task_id: str, final: FinalMessage | None,
                              error: ErrorEvent | None) -> None:
        """bridge 调用：mark_done + notify_parent + 发 AsyncTaskStatus。"""
    async def _on_timeout(self, task_id: str) -> None:
        """TimerHandle 触发：cancel(task_id, reason='timeout')。"""
```

### 状态机

```
                start()
                  │
                  ▼
              ┌─pending──┐
              │          │ (SessionLoop.start() 立即被 dispatch_inbound 触发)
              ▼          │
           running ──────┘
              │
   ┌──────────┼──────────┬────────────┐
   │          │          │            │
   ▼          ▼          ▼            ▼
completed  failed   cancelled    timed_out
(收到       (Error-  (cancel /   (TimerHandle
 Final)     Event)   Timeout)   触发 cancel)
   │          │          │            │
   └──────────┴──────────┴────────────┘
                  │
                  ▼
           finished_at 写入，task.json 落盘，
           parent_sk input_q 投 async-task-result event
```

### 存储

```
<state_root>/
├── default/checkpoint.jsonl              # 普通 session
├── chat-42/checkpoint.jsonl
└── async/                                # AsyncTask 专属根（独立、不跟 session_key 混）
    ├── t_4f9e_a1b2c3d4/
    │   ├── checkpoint.jsonl              # child SessionLoop 复用 JsonlCheckpointStore
    │   └── task.json                     # AsyncTask 完整字段
    ├── t_5b0c_d5e6f7a8/
    │   ├── checkpoint.jsonl
    │   └── task.json
    └── _index.json                       # 可选：内存里 dict[task_id, AsyncTask]，重启从 task.json 重建
```

启动时扫描 `<state_root>/async/*/task.json` 重建 `_tasks` 索引；status=running 的标记为 interrupted（Runtime 重启会丢 child loop，下次 fork 拿不到旧 final；这是可接受降级——历史 final 在 checkpoint 里）。

---

## RuntimeServer 改动

> 只加新方法，不改既有 fan-out / inbound 路径。

```python
class RuntimeServer:
    # 新增：全局 async_task_event 订阅
    # MonoDesk 在 hello 帧 data 里加 "subscribe_async_tasks": true 声明
    # RuntimeServer 记录 _async_task_subscribers: set[ws_conn]
    # AsyncTaskManager.on_event 调到这里 → fanout 给所有 _async_task_subscribers

    async def register_async_task_subscriber(self, ws) -> None: ...
    async def unregister_async_task_subscriber(self, ws) -> None: ...

    # 新增：handle inbound async_task_cancel / async_task_list_query
    async def _handle_inbound(self, ev: InboundEvent) -> None:
        # 现有 inbound 类型 → 走 session_manager.dispatch_inbound
        # 新增两种 → 走 AsyncTaskManager
```

**Routing 关键决策**：`async_task_event` 帧是**全局 fan-out**（不发到单个 session_key 的 last_active_source），因为任务列表是全局 UI 状态；session_key 字段保留只用作「父 session 标识」（让 MonoDesk 把子任务的事件归到哪个父对话里——其实不需要，task_id 就够了；session_key 留作冗余）。

---

## Tool 实现

### `core/loop/tools/fork_task.py`

```python
class ForkTaskTool:
    name = "fork_task"
    schema = {
        "type": "function",
        "function": {
            "name": "fork_task",
            "description": (
                "Fork an async task (subagent by default) that runs independently "
                "in the background. Returns immediately with a task_id. The task "
                "starts with a fresh context (description as the first user turn) "
                "and inherits your full tool registry (including fork_task itself, "
                "supporting nested subagents).\n\n"
                "When the task completes, you receive an <event type='async-task-result'> "
                "in your input. You can also call poll_task(task_ids=[...]) at any "
                "time, or cancel_task(task_id=...) to abort."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Task description (first user turn)."},
                    "meta": {"type": "object", "description": "Arbitrary kv for tagging / UI display."},
                    "kind": {"type": "string", "enum": ["subagent"], "default": "subagent"},
                    "timeout_sec": {"type": "integer", "description": f"Timeout. Default 1800, max {MAX_TIMEOUT_SEC}."},
                },
                "required": ["description"],
                "additionalProperties": False,
            },
        },
    }

    def __init__(self, async_task_manager: AsyncTaskManager,
                 parent_session_key_provider: Callable[[], str]): ...
    async def execute(self, call_id, arguments) -> ToolResult: ...
```

### `core/loop/tools/poll_task.py` + `cancel_task.py`

同样模式：`name` / `schema` / `execute`。`cancel_task` 不真 cancel child loop——它调 `AsyncTaskManager.cancel(task_id, reason="agent")`，manager 统一处理三种 cancel 入口。

### run.py 注册

```python
async_task_mgr = AsyncTaskManager(
    session_manager=session_mgr,
    bridge_factory=AsyncTaskBridge.factory(state_root),
    state_root=state_root,
    on_event=_broadcast_async_event,  # 包装 server.broadcast_async_task_event
    default_timeout_sec=cfg.async_task.default_timeout_sec,
)
tools = ToolRegistry([
    BashTool(runner, paths["workspace"]),
    SkillLoadTool(skill_service),
    MultimodalUnderstandTool(),
    WaitIoTool(),
    budget_tool,
    ForkTaskTool(async_task_mgr, lambda: cfg.session_key),
    PollTaskTool(async_task_mgr),
    CancelTaskTool(async_task_mgr),
])
```

---

## channel 兼容

> 「所有 channel 看到一致的 fork / 状态 / 取消事件；UI 通道最丰富，其他通道走 fallback」。

| channel | async_task_created | async_task_event | async_task_status | async_task_cancel (in) |
|---|---|---|---|---|
| **MonoDesk** | Tasks 面板新增一行 | Tasks 详情页累积 token / tool | Tasks 面板状态更新 + 详情关闭 | 详情页 cancel 按钮（任意时刻） |
| **terminal** | 打印 `[task t_4f9e forked: <description>]` | 折叠打印（流式 status / 工具一行） | `[task t_4f9e completed in 123s]` | 不支持（adapter 不挂 inbound handler；命令调 cancel 用 `/cancel t_4f9e`） |
| **feishu** | 发卡片消息到父对话所在 chat | 流式更新同一张卡片 | 更新卡片状态（completed / cancelled） | 不支持 |
| **textual** | 同 terminal 风格 | 同 terminal 折叠 | 同 terminal | 同 terminal |

**terminal 折叠打印示例**：

```
[task t_4f9e] subagent started: "review this PR"
[task t_4f9e]   thinking…
[task t_4f9e]   bash ls .  → 214ms exit 0
[task t_4f9e]   …done in 47s
```

每个 channel adapter 在 `core/protocol/wire_frames.py` 加新 type handler 是本地事——不需要改 core；这正是「runtime 不感知 channel」的体现。

---

## MonoDesk UI

### 布局（侧栏三页）

```
┌──────────────┬──────────────────────────────────────────────┐
│ Chat         │                                              │
│ Skills       │   页面内容（跟当前一样）                        │
│ ● Tasks (3)  │                                              │
│              │                                              │
│ ─────────── │                                              │
│ (当前 page    │                                              │
│  的子内容)    │                                              │
│              │                                              │
└──────────────┴──────────────────────────────────────────────┘
```

- Sidebar `currentPage: SidebarPage` 加 `"tasks"` 枚举
- Tasks 项右侧小角标：`running` 数（连接 MonoDeskWS 时由 async_task_list 帧驱动更新）
- 进入 Tasks 页时主动发 `async_task_list_query` 拉一次（同时 ws 也持续推变化）

### Tasks 列表页

```
┌────────────────────────────────────────────────────────┐
│ Tasks                                       [+ refresh] │
├────────────────────────────────────────────────────────┤
│ ● running  t_4f9e…  review PR                          │
│             "review this PR for security issues"        │
│             parent: default · 47s / 30min · subagent    │
│                                                          │
│ ◉ completed  t_5b0c…  refactor module                  │
│             "extract auth helper"                       │
│             parent: chat-42 · done in 12s               │
│                                                          │
│ ✕ cancelled  t_8d1f…  long test                         │
│             "run full test suite"                       │
│             parent: default · cancelled at 8min by user  │
└────────────────────────────────────────────────────────┘
```

- 列表项点击 → 进详情页
- 列表项右侧 `Cancel` 按钮（仅 running 显示）→ 发 `async_task_cancel`

### Tasks 详情页

```
┌────────────────────────────────────────────────────────┐
│ ← back    t_4f9e…  review PR         [cancel]           │
│   subagent · parent: default · 47s / 30min              │
│   meta: {pr_url: "...", priority: "high"}              │
├────────────────────────────────────────────────────────┤
│ ● THINKING · 1.2s                                        │
│ ┃ 让我先看 PR diff…                                     │
│ ● bash · gh pr diff 1234 · 214ms                       │
│   $ gh pr diff 1234                                     │
│   …stdout…                                              │
│ ● token ▍ 看到一处 SQL injection…                       │
│ ● bash · rg "raw_query" · 89ms                         │
│ …                                                       │
│ ● DONE · 47s · final: "Found 1 critical issue…"         │
└────────────────────────────────────────────────────────┘
```

- 详情页订阅 `async_task_event`（按 task_id 过滤），用现有 StreamEngine（src/stream/engine.ts）累积——**复用 MonoDesk 的流式渲染管线**，不重写
- 跨链接：详情页里 tool block 跟 Chat 视图同一份 React 组件（`ReasonBlock` / `ToolBlock`）
- 顶部的 `meta` 字段表格化展示

### Chat 页内的 cross-link

fork_task / cancel_task 工具块在 Chat 流里：

```
┌ ● fork_task · t_4f9e… · 47s · [running → open] ─┐
│   description: "review PR"                       │
│   meta: {pr_url: "..."}                          │
└─────────────────────────────────────────────────┘
```

- `t_4f9e…` 文字渲染为可点击 link → 跳 Tasks 详情页
- 状态从 running → completed 时自动 update 文字 + 颜色

实现：Chat 流里 tool_start / tool_end 块按 tool_name 分发——是 fork/cancel/poll_task 时走 TaskBlock 组件，其他工具走现有 ToolBlock。

---

## 实施 Phase

### Phase 1: 协议 + AsyncTaskManager 骨架（核心 + 协议）

- [ ] `core/async_task.py`：`AsyncTask` dataclass + `AsyncTaskManager` + `AsyncTaskBridge`
- [ ] `core/loop/tools/fork_task.py` + `poll_task.py` + `cancel_task.py`
- [ ] `core/protocol/wire_frames.py`：7 个新 FrameType + 编解码 + 单元测试
- [ ] `core/runtime_server.py`：async_task_event 全局 fan-out + 2 个 inbound handler
- [ ] `run.py`：装配 `AsyncTaskManager` + 3 个 tool + 配置段 `[async_task]`
- [ ] 单测：`tests/test_async_task_manager.py`
  - start 立即返回 task_id
  - child SessionLoop 真的跑了（注入 mock llm 看 output_q 收到东西）
  - cancel / timeout / 三种入口统一
  - notify_parent 投到 input_q（mock SessionManager 看 input_q 收到）
  - parent input_q 事件格式正确

### Phase 2: terminal / feishu / textual channel adapter 适配（最小化）

- [ ] terminal adapter 加 5 个新 outbound handler（折叠打印）+ `/cancel <task_id>` command（→ 发 async_task_cancel inbound）
- [ ] feishu adapter 加卡片渲染（用现有 card builder）
- [ ] textual adapter 跟 terminal 同样折叠逻辑

### Phase 3: MonoDesk UI

- [ ] `src/components/Sidebar.tsx`：加 `"tasks"` page + 角标
- [ ] `src/components/TasksPage.tsx`：列表 + 详情（复用 StreamEngine + ToolBlock / ReasonBlock）
- [ ] `src/store/tasks.ts`：task 列表状态（按 task_id 索引，hot update via async_task_event 帧）
- [ ] `src/ws/protocol.ts`：7 个新 type 定义
- [ ] `src/ws/client.ts`：hello 帧带 `subscribe_async_tasks: true`
- [ ] Chat 流 fork_task / cancel_task 工具块改 TaskBlock（跨链接）

### Phase 4: e2e + 文档

- [ ] `tests/test_e2e_async_task.py`：跑一个 subagent 改文件，主 agent poll / cancel，断言
- [ ] `spec/ARCHITECTURE.md` §11 TODO 加 3 项；§13 新增（核心章节：AsyncTask）
- [ ] `spec/README.md` requirements 树加本文件

---

## 风险

- **child SessionLoop 死了父 agent 还活着**：AsyncTaskManager.cancel 必须容错（找不到 task 返 ok；task 已被 GC 也不抛异常）
- **child 跑太慢 + parent idle destroy**：parent_sk idle timeout 不应该级联 cancel child task——child 是独立 session_key，独立 timeout；除非显式调 cancel_task
- **Runtime 重启 + child 中断**：启动时扫描 task.json，status=running → 改 interrupted；final_text / events 落 checkpoint；下次 agent poll_task 能看到
- **嵌套 fork（subagent 再 fork subagent）**：fork_task tool 在 ToolRegistry 里——subagent 跟 parent 共享 registry，天然支持；但要小心 fork 风暴（一个 fork 风暴能起 N×N 个 SessionLoop）。本期不做限制（v0 不优化）；如需可加 `[async_task].max_concurrent` 配置 + AsyncTaskManager 池化
- **wire frame 体积**：`async_task_event` 每条 token 都包成完整 frame——若 child 跑长文本 1k tokens/s，单 conn 上行 1k 帧/s。可接受（MonoDesk StreamEngine 已有 jitter buffer），监控即可，不优化
- **MonoDesk hello 帧 schema 变更**：`subscribe_async_tasks` 是 additive，老 MonoDesk 不发这个字段也能用，RuntimeServer 检测字段不存在时把 conn 加入默认「不订阅」set，OK
- **terminal 折叠打印漏掉信息**：channel 兼容性是「够用」而非「完美」；terminal 用户看压缩日志，MonoDesk 看完整——这是设计意图

---

## 不做的事（明确边界）

- ❌ 多 agent orchestration / agent teams（不引入 shared scratchpad / 共识机制）—— single-parent fan-out 是当前模型
- ❌ AsyncTask 的 deps DAG / 工作流引擎（一个 task 不能依赖另一个 task 完成）
- ❌ 远程 / 跨机器 task 调度（Runtime 进程内，本地资源）
- ❌ per-task cost quota / 配额
- ❌ agent 自动 fork 决策（agent 自己调 tool；本期不做「自动并行」优化）
- ❌ bash long-running 单独 kind（用 `bash` tool + `timeout` 参数已经能做；如用户要 background bash 单独 kind 再迭代）

---

## 附录 A：外部参考（Claude Code / Agent SDK 的对比视角）

调研 v2.1.239 的 Claude Code + Agent SDK + DeepseekHarness 项目源码后，关键对照：

| 维度 | Claude Code | MonoX 本设计 | 选择理由 |
|---|---|---|---|
| **隔离单位** | 进程级：spawn 真实 `claude` CLI 进程 + stdio pipe | 进程内：复用 `SessionLoop` + asyncio task | core「stable kernel 极简」哲学；Runtime 单进程，多 SessionLoop 已经是成熟抽象；不引入进程间通信 / 进程生命周期管理 |
| **父 agent 可见子 agent 中间状态** | 默认**不可见**，仅看到 final result；需 `--forward-subagent-text` 启用 | 默认**全部可见**（child StreamEvent 通过 async_task_event 帧 fan-out） | Runtime 单进程 + 共享 wire 协议 → fan-out 成本接近零；用户友好 |
| **超时控制** | `maxTurns` (turn 数) + `timeout_seconds` | `timeout_sec`（wall-clock）+ hard cap 2h | subagent 是后台任务，wall-clock 更直观；hard cap 防滥用 |
| **中断** | `TaskStop` tool / AbortController / 进程 SIGTERM | `cancel_task` tool / MonoDesk 按钮 / `asyncio.TimerHandle` → 统一 `AsyncTaskManager.cancel()` | 三个入口同一出口，状态机一致 |
| **并发限制** | 默认 20 个（`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`）+ 嵌套深度 3 | **本期不做** | fork 风暴风险已知，留 v0.2+ 优化；本期宁可简单 |
| **持久化** | `~/.claude/projects/<proj>/<sid>/subagents/agent-<id>.jsonl`（per-subagent JSONL） | `<state_root>/async/<task_id>/checkpoint.jsonl` + `task.json`（复用现有 CheckpointStore） | 完全一致的「per-task 独立 checkpoint」模型 |
| **「异步任务」抽象** | **没有统一抽象**——Bash 后台 / Subagent 后台 / Background Session 是三层独立机制 | **统一 AsyncTask 抽象**，subagent 是 `kind="subagent"` | 「subagent 是异步任务的经典场景」是 MonoX 的更高层定位；后续加 `kind="bash_long"` 等无需新机制 |
| **结果回传父 agent** | Agent tool result 一次性返回 final text；背景模式靠 `SendMessage` 跨 session resume | child `FinalMessage` 触发 `AsyncTaskManager.notify_parent` 投 `async-task-result` 事件到父 input_q → 复用 `wait_io` 唤醒 | MonoX 的 `LoopEngine.run` 已经有「新 InboundEvent 唤醒」机制（§7 wait_io 设计），完全复用，零 engine 改动 |
| **跨 session resume** | 支持（agentId 引用，保留完整 conversation） | 不支持（AsyncTask 完成即终结；如需恢复用普通 session_key 单独开） | 复杂度权衡；本期 single-shot only |

### 关键设计选择的外部佐证

1. **「异步任务」统一抽象是 MonoX 走在前面的地方**：Claude Code 三层机制各自为政（Bash 后台是 shell 子进程、Subagent 后台是独立 CLI 进程、Background Session 又是 supervisor 管的完整进程）。MonoX 的「subagent 只是 kind 之一」抽象把三个机制压成一个，可扩展性更强。
2. **「fork 出 child SessionLoop」而非「进程级 spawn」**：MonoX 的 SessionLoop 设计（异步队列 + interrupt + wait_io）已经成熟，复用这条路是「Core 最小改动原则」的最优解。
3. **「统一 cancel 入口」**：Claude Code 的 `TaskStop` 只覆盖 task tool 场景；bash 后台用 `kill`；background session 用 supervisor 命令。MonoX 把三种 cancel 入口合到一个 `AsyncTaskManager.cancel(task_id, reason)`，是简化。
4. **「fan-out 全局可见」**：Claude Code 默认隐藏 child 中间过程（成本是 UI 复杂度 + token 消耗）；MonoX 默认全部 fan-out 是「SessionLoop 在同一进程 + wire 协议已经设计好 fan-out」的零成本产物。
