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
- **`run.py` 装配微改**：`_register` 回调对 `async:` 前缀的 session_key 跳过 `register_outbound_queue`——保证 child output_q 只有 AsyncTaskBridge 一个消费者（见 §数据流「Subagent 执行」）；`finally` 里加 `async_task_mgr.shutdown()`。
- **`core/config.py`**：新增 `[async_task]` 配置段（default/min/max timeout、max_steps）。
- **MonoDesk**：新增 `Tasks` 侧栏页面（紧跟 Chat / Skills 之后）。

### 不改

- **不改 `LoopEngine` 的 ReAct 状态机**。AsyncTask 是 SessionLoop 的特殊用法；唯一的 engine 微改是 tool dispatch 处 set 一个 session-scoped contextvar（fork_task 定位 parent 用，见 §概念「parent_session_key 从哪来」），2 行，不碰状态机。
- **`SessionManager` 的 lazy create / dispatch_inbound / checkpoint 恢复一律不改**；idle sweeper 加一条「react step 运行中不销毁」判断（见 §风险「idle sweep 与长任务」——对普通 session 同样生效，是修现存 bug，不是 async 特判）。
- **不改 `wait_io` 语义**。AsyncTask 完成时通过 `AsyncTaskManager.notify_parent` 投 system event 到父 session 的 input_q，触发父 engine 已有的「新 InboundEvent 唤醒」路径（run() 主循环的 `_race_get` / sub_queue 分支：idle 时直接唤醒；turn 中在下一个 drain 点聚合进当前 turn，不抢占）。
- **不改 `core/protocol/events.py`**。本需求的 5 个 wire dataclass（`AsyncTaskCreated` / `AsyncTaskEvent` / `AsyncTaskStatus` / `AsyncTaskList` / `AsyncTaskSnapshot`）是 wire 层专用，定义在 `wire_frames.py`，不进 `StreamEvent`（先例：hello）。
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
    task_id: str                # "t_" + uuid4().hex[:12]，如 "t_4f9ea1b2c3d4"；UI 截短展示
    kind: str                   # "subagent"（SessionLoop）| "bash_long"（后台 shell 进程）
    description: str            # 第一句任务描述（UI / poll 用）
    parent_session_key: str     # 谁 fork 的（主 session_key）
    child_session_key: str      # "async:" + task_id（subagent 复用 SessionLoop；bash_long 仅作 task.json 目录名）
    status: Literal["pending","running","completed","failed","cancelled","timed_out","interrupted"]
                                # interrupted：Runtime 重启时对 status=running 的终态标记
                                # （child loop 不复存在；poll_task 可见，历史输出在 checkpoint）
    created_at: float
    command: str | None         # bash_long 的 shell 命令（subagent 为 None）
    meta: dict[str, Any]        # 扩展 kv，agent fork 时填
    started_at: float | None    # 实际启动时间
    finished_at: float | None
    timeout_sec: float          # 默认 1800（30min）
    final_text: str | None      # completed 时的交付物（subagent final / bash stdout+stderr）
    error: str | None           # 失败原因
    cancel_reason: str | None   # agent / user / timeout
```

### 两种 kind 的执行路径

| | subagent | bash_long |
|---|---|---|
| 载体 | child SessionLoop（契约 + 首条消息） | `asyncio.create_subprocess_shell`，独立进程组（`start_new_session`） |
| 输出 | FinalMessage → final_text | stdout/stderr → TokenChunk 进 ring buffer + wire，同时落 final_text |
| cancel | 投 interrupt → engine 协作中断 → destroy | `killpg(SIGKILL)` 整组杀 → 等进程收尸 → _finish |
| cwd | 与普通 session 相同 | `bash_cwd`（run.py 传 workspace，与 agent 的 bash tool 同 cwd） |
| timeout | TimerHandle → cancel | 同左 |

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
- 第一条 user message：`{"role": "user", "content": user_input_event_xml(<InboundEvent(kind="message", text=SUBAGENT_CONTRACT + description, source="async_task", event_type="async-task-prompt", meta={...})>)}`——`SUBAGENT_CONTRACT` 是子 agent 契约文本，见下节
- system prompt 跟普通 SessionLoop 一样（共享，不做 subagent 专用 prompt）；子 agent 的行为约束全部由首条消息里的契约承担
- 复用 `JsonlCheckpointStore` 落到 `<state_root>/<child_sk>/checkpoint.jsonl`（即 `async:<task_id>/`，既有 sk 派生规则，见 §存储的路径决策）
- 工具集**完全继承**当前 ToolRegistry（agent 能 fork 时调的所有 tool，subagent 也能调；包括 fork_task 本身——**支持 subagent 嵌套**）
- **单消费者保证**：`SessionManager._create` 会**无条件**调 `outbound_register`，child session 天然会被 RuntimeServer 注册一个 per-session consumer。因此 run.py 的 `_register` 回调必须对 `async:` 前缀 sk 跳过注册——否则 RuntimeServer consumer 跟 AsyncTaskBridge 抢同一个 Queue（asyncio.Queue 多消费者时每个事件只进一个消费者），child 流被随机劈半，且被抢走的一半因 child 无 `last_active_source` 而**静默丢弃**。改完之后 child 的 output_q **只**由 AsyncTaskBridge 消费

### parent_session_key 从哪来（tool 协议没有 session 上下文）

`Tool.execute(call_id, arguments)` 签名里没有调用方信息，ToolRegistry 又是全 session 共享的——fork_task 无法从参数推断「谁在调我」。注入 `lambda: cfg.session_key` 只在单 session 下碰巧正确：`chat-42` 里 fork 会把结果投到 `default`；subagent 里嵌套 fork 会把孙任务投到 `default` 而不是父任务——`notify_parent` 整条链路的正确性依赖这个字段。

**决策：engine 在 tool dispatch 处 set 一个 session-scoped contextvar**（`core/loop/engine.py` 的 `await tool.execute(...)` 两侧，各 1 行）：

```python
# engine._react tool dispatch 处：
_token = _current_session_key.set(self._session_key)
try:
    result = await tool.execute(call_id, args)
finally:
    _current_session_key.reset(_token)
```

fork_task 执行时用 `current_session_key()` 读 contextvar。这是「不改 LoopEngine」的唯一例外——不动状态机，只是把 engine 本来就有的信息（session_key）暴露给 tool 层。contextvar 天然跟随 asyncio 任务树：subagent 嵌套 fork 时拿到的是 child 自己的 sk，嵌套语义自动正确。

### 子 agent 契约（首条消息文本）

共享的交互式 system prompt 会教 agent「turn 结束调 wait_io」，而 bridge 的完成判据是「第一个 FinalMessage」——不写契约的话，agent 中途反问 / 干一半就 wait_io，父会收到 `completed` + 半成品。因此 fork 的首条消息文本固定为契约 + description：

```
You are running as an autonomous async task (subagent). Contract:
- Work to completion in this single turn. Do NOT ask clarifying questions.
- Do NOT call wait_io mid-task. Your final message IS the deliverable — make it a
  self-contained summary of findings / changes.
- You have the full tool registry, including fork_task for nested subagents.

Task: <description>
```

契约文本是 `core/async_task.py` 里的常量（`SUBAGENT_CONTRACT`），单测断言它出现在首条消息里。
外层由 engine 的 `user_input_event_xml` 包装成 `<event … event_type="async-task-prompt">`，
契约里不嵌套 XML。`max_steps` 本期沿用 SessionManager 全局配置（child 不做 per-task 覆盖，
避免 SessionManager 感知 async 语义）；subagent 步数普遍偏多，不够就全局调大。
`[async_task]` 配置段只含 timeout 三项（default / min / max）。

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
                │    kind="system"            # runtime 内部通知，非用户输入
                │    text="Async task <task_id> (subagent) completed in 47s.\n\nResult:\n<final_text，超 8000 字截断>"
                │    source="async_task"
                │    event_type="async-task-result"
                │    meta={"task_id": ..., "status": ..., "kind": ...}
                │    （engine 侧 user_input_event_xml 包装后，LLM 看到的是
                │      <event kind="system" channel="async_task" event_type="async-task-result">…</event>
                │      ——结构化信息走 XML attrs + meta，不做嵌套 XML）
                │
                ▼
             SessionManager.dispatch_inbound(ev)
                - 找到 parent_sk 的 SessionLoop（不存在时 lazy create + 复活 checkpoint）
                - put 到 input_q
                - 若父 engine 在 wait_io：LoopEngine.run 第 141 行 `sub_queue.get()` 拿到 → 进入 react → 把 XML 解析为 user message → LLM 看到结果
```

这就是复用了 wait_io 的「新事件唤醒」机制，不改 engine 一行代码。

> **完成判据**：child 的第一个 FinalMessage。子 agent 契约（见 §概念）保证它只在交付时结束
> turn；若 child 违约中途 wait_io，任务以「completed + 半成品 final」收场——接受该降级，
> 不引入 turn 计数等复杂判据。

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
       │  1) 向 child input_q 投 InboundEvent(kind="interrupt", source="async_task")
       │     —— 复用 engine 现有协作中断：cancel 当前 step_task → 回滚 _messages/_step_idx
       │     → StatusChange(idle)。
       │     **不直接 task.cancel() SessionLoop.task**：cancel run() 不会传播到正在跑的
       │     step_task（asyncio cancel 不跨任务传播），会留下孤儿 react 继续调 LLM / 写
       │     checkpoint / 往无人消费的 output_q 堆事件；且绕过 engine 的统一清理路径。
       │  2) 等 child 回 idle（bridge 收到 StatusChange(state="idle")，带 5s 超时兜底），
       │     再 destroy child session——loop task 此时退出是安全的
       │  3) mark_done(task_id, status="cancelled"/"timed_out", cancel_reason=reason)
       │  4) notify_parent(...)：投 system-notify 事件到父 input_q，让父 agent 知道「task 已取消」
       │  5) 发 async_task_status wire 帧到所有 MonoDesk
       │  6) bridge task 自然结束（output_q 关闭）
```

### Poll（agent 主动查状态）

```
poll_task()                                  ← 不带参数：全量列出（跨 parent，
       │                                        与 MonoDesk Tasks 面板同口径；
       │                                        任务列表是全局 UI 状态）
poll_task(task_ids=["t_4f9e", "t_..."])      ← 指定任务
poll_task(status=["running"])                ← 按状态过滤
       │
       ▼
对每个 task_id 调 AsyncTaskManager.get(task_id)；无 task_ids 走 list(filter)
       │
       │  对每个 task：
       │    - 读内存 _tasks → AsyncTaskSummary（副本）
       │    - running 的附带 events ring buffer 最近 K 条 → 摘要
       ▼
返回 ToolResult(stdout=json.dumps(summaries, ...))

agent 想拿完整输出：用 poll_task 后再决定要不要 cancel 或者 fork 新的 subagent 处理结果
```

---

## 协议：wire frame 扩展

> 全部集中在 `core/protocol/wire_frames.py:FrameType`，保持「14 + 7 = 21 个 type 集中定义」的现有约定（现有 14 个：10 个 StreamEvent 出站 + hello + user_input / command / interrupt 3 个入站）。

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
        state_root: Path,                  # task.json 落 <state_root>/async:<task_id>/task.json
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

    def get(self, task_id: str) -> AsyncTask | None:
        """返回副本（`dataclasses.replace` / `copy`）——内部记录可变，不外借引用。"""
    def list(self, *, parent_session_key: str | None = None,
             status: list[str] | None = None) -> list[AsyncTask]:
        """同上，返回副本列表。"""
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

重启恢复路径：启动扫描把 status=running → interrupted（不经上图迁移，直接改写 + 落盘 task.json）。

### 存储

```
<state_root>/
├── default/checkpoint.jsonl              # 普通 session
├── chat-42/checkpoint.jsonl
├── async:t_4f9ea1b2c3d4/                 # child 目录 = <state_root>/<child_sk>/（既有 sk 派生规则，无特判）
│   ├── checkpoint.jsonl                  # child SessionLoop 复用 JsonlCheckpointStore
│   └── task.json                         # AsyncTask 完整字段（与 checkpoint 同目录）
└── async:t_5b0cd5e6f7a8/
    ├── checkpoint.jsonl
    └── task.json
```

> **路径决策**：child_sk = `async:<task_id>`，`SessionManager._create` 的既有规则就是
> `<state_root>/<sk>/checkpoint.jsonl`，直接沿用（目录名带冒号，macOS / Linux 合法；goal.md
> 明确不兼容 Windows）。**不**为 `<state_root>/async/<task_id>/` 这种美观目录在 `_create` 里
> 加特判。traces 同理落 `traces_root/async:<task_id>/`。
>
> **task.json 写入时机**：`start()` 成功后立即写（status=running）——只等 mark_done 才写的话，
> running 中 crash 重启时连 task 都发现不了（checkpoint 在、task.json 不在）；之后每次状态
> 迁移重写。

启动时扫描 `<state_root>/async:*/task.json` 重建 `_tasks` 索引；status=running 的改标记为
`interrupted`（Runtime 重启会丢 child loop，下次 fork 拿不到旧 final；可接受降级——历史输出在
checkpoint 里，poll_task 能看到 interrupted 状态 + 已有摘要）。

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

    def __init__(self, async_task_manager: AsyncTaskManager): ...
    # parent_session_key 不注入 provider——execute 时从 current_session_key()
    # contextvar 读（见 §概念「parent_session_key 从哪来」，嵌套 fork 自动正确）
    async def execute(self, call_id, arguments) -> ToolResult: ...
```

### `core/loop/tools/poll_task.py` + `cancel_task.py`

同样模式：`name` / `schema` / `execute`。`cancel_task` 不真 cancel child loop——它调 `AsyncTaskManager.cancel(task_id, reason="agent")`，manager 统一处理三种 cancel 入口。

### run.py 注册

```python
# 既有 _register 回调加前缀过滤（保证 child output_q 单消费者）：
async def _register(sk: str, q: asyncio.Queue) -> None:
    if sk.startswith("async:"):
        return  # child session：不注册 RuntimeServer consumer，output_q 归 AsyncTaskBridge
    await server.register_outbound_queue(sk, q)

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
    ForkTaskTool(async_task_mgr),
    PollTaskTool(async_task_mgr),
    CancelTaskTool(async_task_mgr),
])

# 退出清理（main 的 finally，先于 session_mgr.stop()）：
#   await async_task_mgr.shutdown()   # cancel 所有 running task + flush task.json
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

### Phase 1: 协议 + AsyncTaskManager 骨架（核心 + 协议）✅ 已完成（2026-08-28）

- [x] `core/async_task.py`：`AsyncTask` dataclass + `AsyncTaskManager` + `AsyncTaskBridge`（含 `SUBAGENT_CONTRACT` 常量）
- [x] `core/loop/tools/fork_task.py` + `poll_task.py` + `cancel_task.py`
- [x] `core/protocol/wire_frames.py`：7 个新 FrameType + 编解码 + 单元测试
- [x] `core/runtime_server.py`：async_task_event 全局 fan-out + 2 个 inbound handler
- [x] `core/loop/engine.py`：tool dispatch 处 set `_current_session_key` contextvar（2 行）
- [x] `core/session_manager.py`：idle sweeper 跳过「react step 运行中」的 session
- [x] `run.py`：装配 `AsyncTaskManager` + 3 个 tool + `_register` 前缀过滤 + `shutdown()` 接线 + 配置段 `[async_task]`
- [x] 单测：`tests/test_async_task_manager.py`
  - start 立即返回 task_id；task.json 在 start 时即落盘
  - child SessionLoop 真的跑了（注入 mock llm 看 output_q 收到东西）；首条消息含 SUBAGENT_CONTRACT
  - cancel / timeout / 三种入口统一；cancel 走 interrupt 路径，结束后无孤儿 step（mock llm 挂起 + cancel，断言无后续 LLM 调用、checkpoint 无中间态）
  - 嵌套 fork：child 里 fork，孙任务 parent_session_key == child sk（contextvar 生效）
  - idle sweeper：step 运行中 > idle_timeout 不销毁；真正空闲的照常销毁
  - notify_parent 投到 input_q（mock SessionManager 看 input_q 收到）
  - parent input_q 事件格式正确

### Phase 2: terminal / feishu / textual channel adapter 适配（最小化）✅ 已完成（2026-08-28）

- [x] terminal adapter 加折叠打印（`handle_raw_frame`）+ `/cancel <task_id>` command（→ `raw_outbound` 队列 → async_task_cancel inbound 帧）
- [x] feishu adapter 卡片渲染（created 发卡 + status 终态回写；中间事件不刷防频控）
- [x] textual adapter 跟 terminal 同样折叠逻辑
- [x] 基础设施：`RuntimeWSClient.set_raw_frame_handler`（原始帧旁路，Channel 协议本体不变）+ `_runtime.pump_raw_inbound`（duck-type `raw_outbound` 队列）
- [x] 单测：terminal /cancel 帧构造 + 折叠打印（`tests/test_terminal_channel.py`）

### Phase 3: MonoDesk UI ✅ 已完成（2026-08-28，另一仓）

- [x] `src/ws/protocol.ts`：7 个新 type 定义（+ snapshot_query 预留注释）
- [x] `src/ws/client.ts`：hello 帧带 `subscribe_async_tasks: true` + `cancelTask` / `queryTaskList`
- [x] `src/store/tasks.ts`：TasksStore（byId + per-task 100 条 ring buffer + onFrame 帧级订阅）
- [x] `src/components/Sidebar.tsx`：`"tasks"` page + running 角标
- [x] `src/components/TasksPage.tsx`：列表（running 置顶）+ 空态 + refresh
- [x] `src/components/TaskDetailPage.tsx`：详情（独立 StreamEngine 实例回放 + 实时，复用 Conversation 渲染）
- [x] `src/components/TaskBlock.tsx` + `Conversation.tsx` 路由：Chat 流 fork/cancel/poll 工具块 cross-link
- [x] StatusBar「N tasks running」入口
- [x] 单测：`src/store/tasks.test.ts` + `src/components/TasksPage.test.tsx`

### Phase 4: e2e + 文档 ✅ 已完成（2026-08-28）

- [x] `tests/test_e2e_async_task.py`：真 RuntimeServer（随机端口 ws）+ 全装配——hello 订阅 → user_input 驱动 agent fork → created 帧 fan-out → cancel inbound → 协作中断 → status 帧 + 父通知 + list query
- [x] `spec/ARCHITECTURE.md` §13 新增（核心章节：AsyncTask）
- [x] `spec/README.md` requirements 树加本文件

---

## 风险

- **child SessionLoop 死了父 agent 还活着**：AsyncTaskManager.cancel 必须容错（找不到 task 返 ok；task 已被 GC 也不抛异常）
- **idle sweep 会杀长任务**：`last_active_ts` 只在 dispatch_inbound 更新，child 跑 30min turn 期间无任何 inbound → 300s 就会被 sweep 中杀（默认 timeout 1800s 形同虚设）。这是**现存 bug**：普通 session 跑 20min 长命令 turn 同样中招。修法：engine 暴露 `step_task is not None` 的 busy 状态，sweeper 跳过运行中的 session（Phase 1 已列入）。parent 与 child 依旧解耦：parent 真正空闲超时被 destroy 不影响 child；child 完成时 notify_parent 走 lazy create 复活 parent
- **cancel 路径的僵尸 step 风险**：若误用 `SessionLoop.task.cancel()`，cancel 不会传播到正在跑的 step_task（asyncio 语义），孤儿 react 继续调 LLM / 写 checkpoint，且可能停在「assistant.tool_calls 已落、tool result 未落」的中间态——下次 restore 后 LLM API 拒收。本设计统一走 interrupt 队列（见 §数据流「Cancel / Timeout」），单测覆盖该路径
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
- ~~❌ bash long-running 单独 kind~~ → ✅ 已实现（2026-08-28）：`kind="bash_long"` + `command` 参数，独立进程组可 kill；见「两种 kind 的执行路径」

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
| **持久化** | `~/.claude/projects/<proj>/<sid>/subagents/agent-<id>.jsonl`（per-subagent JSONL） | `<state_root>/async:<task_id>/checkpoint.jsonl` + `task.json`（复用现有 CheckpointStore） | 完全一致的「per-task 独立 checkpoint」模型 |
| **「异步任务」抽象** | **没有统一抽象**——Bash 后台 / Subagent 后台 / Background Session 是三层独立机制 | **统一 AsyncTask 抽象**，subagent 是 `kind="subagent"` | 「subagent 是异步任务的经典场景」是 MonoX 的更高层定位；后续加 `kind="bash_long"` 等无需新机制 |
| **结果回传父 agent** | Agent tool result 一次性返回 final text；背景模式靠 `SendMessage` 跨 session resume | child `FinalMessage` 触发 `AsyncTaskManager.notify_parent` 投 `async-task-result` 事件到父 input_q → 复用 `wait_io` 唤醒 | MonoX 的 `LoopEngine.run` 已经有「新 InboundEvent 唤醒」机制（§7 wait_io 设计），完全复用，零 engine 改动 |
| **跨 session resume** | 支持（agentId 引用，保留完整 conversation） | 不支持（AsyncTask 完成即终结；如需恢复用普通 session_key 单独开） | 复杂度权衡；本期 single-shot only |

### 关键设计选择的外部佐证

1. **「异步任务」统一抽象是 MonoX 走在前面的地方**：Claude Code 三层机制各自为政（Bash 后台是 shell 子进程、Subagent 后台是独立 CLI 进程、Background Session 又是 supervisor 管的完整进程）。MonoX 的「subagent 只是 kind 之一」抽象把三个机制压成一个，可扩展性更强。
2. **「fork 出 child SessionLoop」而非「进程级 spawn」**：MonoX 的 SessionLoop 设计（异步队列 + interrupt + wait_io）已经成熟，复用这条路是「Core 最小改动原则」的最优解。
3. **「统一 cancel 入口」**：Claude Code 的 `TaskStop` 只覆盖 task tool 场景；bash 后台用 `kill`；background session 用 supervisor 命令。MonoX 把三种 cancel 入口合到一个 `AsyncTaskManager.cancel(task_id, reason)`，是简化。
4. **「fan-out 全局可见」**：Claude Code 默认隐藏 child 中间过程（成本是 UI 复杂度 + token 消耗）；MonoX 默认全部 fan-out 是「SessionLoop 在同一进程 + wire 协议已经设计好 fan-out」的零成本产物。
