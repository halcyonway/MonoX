# interrupt: LoopEngine 中断机制

> 本文只讨论 **用户触发的运行中打断**（MonoDesk 停止按钮 / channel 发 interrupt 帧）。
> Runtime 级关闭（channel 关闭 → engine 干净退出）见 [shutdown.md](./shutdown.md)，
> 两者信号源不同、语义不同，不合并。

## 问题

现状 LoopEngine 只有一条输入队列，三种 kind 全部混流：

```
SessionLoop.input_q（RuntimeServer 投递，唯一入站口）
        │ pumper（原样搬运）
        ▼
    sub_queue ── message / command / interrupt 混在一条 FIFO
```

interrupt 的消费时机散在三处：

| 场景 | 行为 | 问题 |
|---|---|---|
| 空闲等输入 | `sub_queue.get()` 拿到 interrupt → 发 idle | ✅ 正常 |
| react 跑着 | `_select(sub_queue, step_task)` 先到先处理；interrupt 先到 → cancel step_task | ⚠️ 能打断，但优先级靠 FIFO 排队运气 |
| `_react` 内部 drain | 两处 `_drain(sub_queue)` 聚合新消息，**不看 kind** | ❌ 真 bug |

第三处是真 bug：躲过 `_select` 时机的 interrupt（如 step 刚启动瞬间到达）会被
`user_input_event_xml()` 包装成一条空文本 user message 追加进对话上下文并写 checkpoint
——既没有打断，还污染了 messages 历史。

## 目标

1. interrupt 具有结构化最高优先级：任何时候到达都尽快生效，不依赖 FIFO 顺序
2. interrupt 永远不会进入 LLM 对话上下文
3. 清理逻辑（回滚 / metric / trace / idle）收敛在一处，不复制
4. wire 协议、SessionManager、InboundEvent schema 零变化

## 设计

### 1. 引擎边界分流

pumper 按 kind 分流，engine 内部两条队列：

```python
interrupt_q: asyncio.Queue[InboundEvent] = asyncio.Queue()
sub_q:      asyncio.Queue[InboundEvent] = asyncio.Queue()

async def pumper():
    while True:
        ev = await input_queue.get()
        if ev.kind == "interrupt":
            await interrupt_q.put(ev)
        else:
            await sub_q.put(ev)
```

Runtime 协议层不动：还是投 session 单一入口，分流发生在 engine 内部。

### 2. 检查点（全部 get_nowait 非阻塞）

| # | 位置 | 动作 |
|---|---|---|
| C1 | `run()` 主循环每轮开头 | 扫 `interrupt_q`，命中走统一清理 |
| C2 | `_react` 每个 step 开头、drain 新消息之前 | 命中 → 中止本轮，返回哨兵值 |
| C3 | `_react` LLM 流式消费循环内（每个 chunk） | 命中 → 立即中止流，返回哨兵值 |

C3 是新增能力：此前流式中途只能靠 `_select` 抢先 + task.cancel 打断；
现在毫秒级协作中止，且对「LLM 连接正常但模型长时间不吐字」同样有效。
原 `_select` 抢先逻辑保留作兜底（覆盖卡死在连接建立等 chunk 循环根本没跑起来的情形）。

### 3. 清理收敛在 run()

`_react` 发现中断只做一件事：快速返回哨兵值 `_INTERRUPTED`（模块级 str 常量），
不碰 messages / checkpoint / metric。

所有清理仍由 `run()` 统一执行（与既有 task.cancel 路径共用同一套）：

```
回滚 messages → self._msgs_before 快照
step_idx -= 1；session_metric.drop_last()
trace end_run(status="cancelled")
output: StatusChange(idle)
step_task = None；回到主循环等下一条输入
```

`run()` 收到 step 完成的 final_text 时：
- `final_text == _INTERRUPTED` → 走上述清理
- 正常字符串 → 走既有 FinalMessage 分支

`task.cancel()` 外部路径保留（覆盖 LLM 连接 hang 死这类无法协作的场景），
其 except CancelledError 分支与哨兵路径调用同一个清理函数，避免两份复制。

### 3.1 连发 interrupt 的合并语义

- **同一瞬时的 burst**（处理开始前排队的余量）：`take_interrupt()` /
  `_race_get` 一次性吞掉，只产生一次打断反馈
- **先后到达的 interrupt**：各自触发一次反馈——每次点击都是一次明确的打断请求，
  逐条响应是预期行为而非泄漏

用公式说：N 条 interrupt 在时间上聚成 K 个 burst（K ≤ N），产生 K 次 idle 反馈。

### 4. tool 执行阶段的中断

C3 只覆盖 LLM 流式段。tool 执行（可能跑长 bash）维持现状：由 `_select` 兜底
cancel ——tool 内部已是 asyncio subprocess，cancel 可传播杀进程。本文档不扩展。

## 不做的事

- 不给 `InboundEvent` 加字段，不改 wire_frames
- 不改 SessionManager / RuntimeServer 投递方式
- 不引入「部分中断」（如"只停 tool 不停 turn"）；一次 interrupt = 打断当前整个 step
- CLI 前端表现不在本范围

## 验证

`tests/test_interrupt.py`（新增），核心场景：

1. react 跑着发 interrupt → 流式段被打断；messages 回滚到快照；收到 idle；无 FinalMessage
2. interrupt 在 step 启动瞬间到达 → 不再被当成空 user message 写进 checkpoint
3. 连发多条 interrupt → 同批合并，反馈次数 ≤ 打断条数；引擎保持可用
4. interrupt 后紧跟新 message → message 被正常聚合进下一轮 react
5. tool 长执行中发 interrupt → 由 cancel 路径打断（现有行为回归）
6. 空 input 上直接发 interrupt（空闲态）→ idle，无副作用

## 进度

- 设计：本文档
- 实现：未开始
