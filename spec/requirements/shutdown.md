# shutdown: engine 响应 channel 关闭

## 问题

当前（v0.9）用户从 channel（terminal / textual）输入 `exit` 或按 Ctrl+C：
- TextualChannel.stop() 让 App 退出、_stop.set、in_q drain 干净
- 但 `core.loop.engine.run()` 在 `await input_queue.get()` 永远阻塞
- result：`asyncio.gather(gateway.run(), loop.run(...))` 不返回，整个 process 不退出
- user 必须再按一次 Ctrl+C 把整个 process kill

TerminalChannel 同样行为。

## 目标

channel 关闭时干净终止 engine，进而终止整个 runtime。

## 设计

最小侵入方案：**给 engine 注入 shutdown_event，engine.run 时 select race**。

### 接口改动

`core.loop.engine.LoopEngine.run(input_queue, output_queue, *, shutdown_event=None)`：

- `shutdown_event: asyncio.Event | None`：外部信号；set 时 engine 应在最近 safe point 退出
- 当 input_queue.get() 跟 shutdown_event.wait() race：先醒先处理

```python
async def run(self, input_queue, output_queue, *, shutdown_event=None):
    ev = asyncio.create_task(input_queue.get())
    sd = asyncio.create_task(shutdown_event.wait()) if shutdown_event else None
    done, pending = await asyncio.wait(
        {ev, sd} if sd else {ev},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if sd and sd in done:
        # shutdown
        return
    ev = ev.result()
    ...
```

或更简洁：用 `asyncio.wait_for(input_queue.get(), timeout=0.5)` 循环，每次检查 shutdown。

### Gateway 装配

`core.gateway.dispatcher.Gateway`：

- 监听 channel 的 `_stop`（Channel 协议没暴露 _stop，但可以加一个 `closed()` 方法或 `wait_closed()` async method）
- 或更简单：Channel 实现 `Channel` Protocol 时同时持有一个 `asyncio.Event`，Gateway 注入 `shutdown_event` 给 engine

### Channel 协议扩展

可选：给 `Channel` 加 `wait_closed() -> asyncio.Event`（返回内部 close event）。

但更简单的：**让 Channel.stop() 时主动 set 一个 shared shutdown_event，engine 通过 Gateway 注入**。

### run.py 装配

```python
shutdown = asyncio.Event()
channel = build_channel(cfg, debug)
# 装配：channel 关闭时 set shutdown
channel.on_close = shutdown.set   # 或 callback 注册

loop = LoopEngine(..., shutdown_event=shutdown)
gateway = Gateway(channel, loop_input=iq, loop_output=oq)
```

## 验证

```python
async def test_exit_shuts_down_engine():
    ch = TerminalChannel("test")
    loop = LoopEngine(..., shutdown_event=shutdown)
    gw = Gateway(ch, ...)
    gw_task = create_task(gw.run())
    loop_task = create_task(loop.run(iq, oq, shutdown_event=shutdown))

    await ch.start()
    ch.submit_user_input("exit")
    await asyncio.wait_for(asyncio.gather(gw_task, loop_task), timeout=5)
    # 期望：gather 在 5s 内完成，无 TimeoutError
```

## 风险

- engine 内的 LLM 流式调用（`async for chunk in llm.stream(...)`）也应该响应 shutdown —— 加 `await asyncio.shield(...)` 之外的 try/cancel
- tool execute 可能在跑 bash（subprocess），也得 cancel
- `wait_for` + timeout 轮询方案在 idle 期反应 < 0.5s，对体感够用

## 进度

- 设计：本文档
- 实现：未开始（v1.0 候选）