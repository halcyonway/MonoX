# fix-tool-end-on-interrupt: interrupt 后 tool block 必须有 ToolEnd 收尾

> **Bug 类**。本 spec 是 [interrupt.md](./interrupt.md) 的**补丁**：
> interrupt 协作中止已经覆盖 LLM 流式段（§C3），但 tool 执行段的中断还缺**两步收尾**——
> ToolEnd 事件不发出 + bash 子进程不被杀。本 spec 专门修这两个洞。

## 问题

`core/loop/engine.py:645` 处的 tool 执行路径：

```python
await output_queue.put(ToolStart(name=name, args=args, call_id=call_id))
...
try:
    result = await tool.execute(call_id, args)
except Exception as exc:
    # 自定义 tool 异常兜底 —— 但 CancelledError 不是 Exception
    ...
# ⚠️ ToolEnd 这一行只在 try/except 正常路径执行
await output_queue.put(ToolEnd(name=name, result=result, latency_ms=latency_ms))
```

用户按 MonoDesk 停止按钮 → `engine._step_task.cancel()` →
`await tool.execute(...)` 抛 `asyncio.CancelledError`（不是 Exception）→
整个 `_react` 协程被取消、返回 → **ToolEnd 永远不被发出**。

实际后果：

1. **UI 工具块永远卡死**：MonoDesk 收到 ToolStart 把 tool block 切到 `running` 状态，
   启动本地计时器；`t-dot` 一直脉冲、`t-args-pending` 一直闪、`elapsed` 一直递增。
   `engine` 这边已经 `finalize_aborted("cancelled")` 发 `StatusChange(idle)` 了，
   但 tool block 收不到 ToolEnd → 永远停在 `state: "running"`。
2. **trace 缺一条 cancelled act span**：`record_act_span` 也没被调用，
   trace 面板看不到这条 tool 被中断。
3. **bash 子进程继续跑**：`core/sandbox/bash_runner.py` 创建的
   `asyncio.create_subprocess_shell` 在 `await proc.communicate()` 抛
   CancelledError 时**不会**自动杀进程——`proc` 被遗忘，子进程变孤儿继续跑。
   用户 UI 上 `t-latency` 持续累加也可能就是孤儿进程的真实耗时。

## 目标

1. 中断发生后，UI 上的所有 `state: "running"` tool block 必须在合理时间内变 done 状态（badge = `cancelled`）
2. trace 必须记一条 `status: "cancelled"` 的 act span
3. 中断时正在跑的 bash 子进程必须被 kill

## 设计

### 1. engine.py：在 `tool.execute()` 抛 CancelledError 时补发 ToolEnd

`core/loop/engine.py:644-668` 改写为：

```python
_cv_token = _current_session_key.set(self._session_key)
try:
    result = await tool.execute(call_id, args)
except asyncio.CancelledError:
    # 中断：用户打断 / 上游 cancel 传播到这里。ToolStart 已经发了但 ToolEnd 没发，
    # UI 工具块会卡在 running —— 必须补一条 cancelled ToolEnd 把状态收尾。
    _current_session_key.reset(_cv_token)
    latency_ms = int((time.monotonic() - t0) * 1000)
    cancelled_result = ToolResult(
        call_id=call_id,
        status="cancelled",
        stdout="",
        stderr="[interrupted] tool execution cancelled by user",
        exit_code=-1,
    )
    await output_queue.put(ToolEnd(
        name=name,
        result=cancelled_result,
        latency_ms=latency_ms,
    ))
    if self._traces is not None and self._current_turn_id is not None:
        await self._traces.record_act_span(
            self._current_turn_id,
            tool_name=name,
            args=args,
            result=_tool_result_to_dict(cancelled_result),
            latency_ms=latency_ms,
            status="cancelled",
        )
    raise  # 继续传播，让 step_task 走 finalize_aborted("cancelled")
except Exception as exc:
    # 原本的兜底：自定义 tool 直接 raise（非返回 status=error）
    result = ToolResult(
        call_id=call_id,
        status="error",
        stdout="",
        stderr=f"{type(exc).__name__}: {exc}",
        exit_code=-1,
    )
    tool_status = "error"
finally:
    _current_session_key.reset(_cv_token)
```

要点：

- `CancelledError` 必须在 `Exception` **之前**匹配（基类关系：`CancelledError` 继承自 `BaseException`，Python 3.8+ 不属于 `Exception`，所以独立 catch 是必要的）
- `_current_session_key` 在两个分支都要 reset —— 放进 `finally`
- catch 块里 `raise` 必须保留：让 `_react` 协程被取消，外层 `run()` 才能走 `finalize_aborted("cancelled")` 关 trace + 回 idle
- `record_act_span(status="cancelled")` —— 跟现有 `ok` / `error` 三态对齐，
  `core/observability/types.py:62` 的 Literal 类型已经支持

### 2. bash_runner.py：CancelledError 路径必须 kill subprocess

`core/sandbox/bash_runner.py:29-34`：

```python
try:
    stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
except asyncio.TimeoutError:
    proc.kill()
    await proc.wait()
    return SandboxResult(stdout="", stderr=f"timeout after {timeout}s", exit_code=124)
# ⚠️ 没处理 CancelledError —— subprocess 变孤儿
```

改成：

```python
try:
    stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
except asyncio.TimeoutError:
    proc.kill()
    try:
        await proc.wait()
    except Exception:
        pass
    return SandboxResult(stdout="", stderr=f"timeout after {timeout}s", exit_code=124)
except asyncio.CancelledError:
    # 上游 interrupt cancel 传播到 communicate() —— 必须主动 kill 否则子进程变孤儿
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    except Exception:
        pass
    raise  # 继续传播，让上层 ToolEnd 收尾
```

要点：

- `proc.kill()` 之前先 try/except `ProcessLookupError`（进程可能已经自然退出）
- `await proc.wait()` 之后 try/except 兜底（避免僵尸 / 拖时间）
- `raise` 保留 CancelledError 让上层的 engine CancelledError 收尾逻辑生效

### 3. 兼容性

- 其他 tool（wait_io / fork_task / poll_task / multimodalunderstand 等）的 `tool.execute()` 实现也是同模式：抛 CancelledError → engine.py 新增的 catch 兜住。
- 旧 client 不识别 `result.status="cancelled"` 也无碍：`ToolResultData.status` 是 Literal，已经定义过这个值（[wire_frames.py:153](./../core/protocol/events.py)）。
- styles.css 已有 `.t-badge.cancelled` 样式 ([styles.css:384](../../../MonoDesk/src/styles.css))。

## 不做的事

- 不改 `interrupt.md` 描述的 C1/C2/C3 三处检查点 —— 协作机制不动
- 不改 `core/loop/engine.py` 的 `handle_interrupt()` 逻辑 —— 那只是 cancel 触发点
- 不引入"只取消 tool 不取消 turn"的部分中断语义
- 不动 LLM 流式段（C3 已覆盖）

## 验证

新增 `tests/test_interrupt_tool_end.py`：

1. **engine 路径**：mock 一个 tool，execute 抛 CancelledError → 断言 output_queue 收到 ToolEnd（status=cancelled）+ trace.record_act_span 被调（status=cancelled）+ CancelledError 继续 raise 给外层
2. **bash_runner 路径**：mock 一个慢 bash（`sleep 60`）→ 异步 cancel run() → 断言 proc 被 kill、proc.wait() 完成、subprocess 不再存在
3. **回归**：现有 `tests/test_interrupt.py` 5 个场景全部不破
4. **手动**：MonoDesk 启动 long bash → 按 STOP → tool block 在 1s 内变 cancelled badge、timer 停

## 进度

- 设计：本文档
- 实现：未开始