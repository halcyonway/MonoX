# llm-error-recovery: LLM 网络错不 abort run，让 ReAct 自己 retry / 决策

> **Bug 类**。本文定义 **LLM 调用瞬时错误**（HTTP 4xx/5xx、连接超时、
> connection reset、read timeout）的恢复策略：**自动 retry + system note 反馈**，
> 不发 fatal `ErrorEvent("llm_error")`，不 abort run。这是 [interrupt.md](./interrupt.md)
> 之外的**第二条"不能让 run 挂掉"的路径**——interrupt 是用户主动打断，本 spec
> 是网络瞬时抖动 / 网关偶发错误。

## 问题

LLM provider 调用失败时（minimax 网关偶发 HTTP 400、cloudflare 5xx、
连接池耗尽超时），当前 `core/loop/engine.py:247-258` 的兜底：

```python
except Exception as exc:
    _log.exception("react step failed: %r", exc)
    await output_queue.put(ErrorEvent(
        code="llm_error",
        msg=f"{type(exc).__name__}: {exc}",
        retryable=True,
    ))
    await finalize_aborted("error")
    self._step_task = None
    continue
```

行为：

1. **整条 run 被杀掉** —— `finalize_aborted("error")` 回滚 messages、
   `end_run(status="error")`、发 `StatusChange(idle)`。UI 永远 idle。
2. **fatal ErrorEvent** —— MonoDesk 显示底部大红框 `❌ llm_error — HTTPSStatusError: Client error '400 Bad Request'...`
   长串 Mozilla 链接；用户看到的全是噪音。
3. **已经发出去的 tool 卡 running** —— react step 抛异常前可能已经
   `output_queue.put(ToolStart(...))`，但 `ToolEnd` 永远不发出来（见
   [fix-tool-end-on-interrupt.md](./fix-tool-end-on-interrupt.md) 同款问题），
   MonoDesk 那个 tool block 一直 `elapsed` 累加。
4. **LLM 没有机会自我恢复** —— ReAct 哲学是 tool 错反馈给 LLM 让它换路径；
   LLM 自身错也是同理（让它少调一次、改 prompt、解释给用户）。abort 直接剥夺
   LLM 决策机会。

用户原话：「tool报错不应该让agentloop挂掉，请你定位问题，最多提示一个tool失败就行了啊，然后交给agent自己处理。」

## 目标

1. **LLM 网络错自动 retry**：HTTP 4xx/5xx、连接超时、connection reset → 自动重试 3 次（指数退避 1s/2s/4s），不给用户看中间错
2. **retry 全部失败**：把异常当 `role=user` system note 反馈进 messages，让 LLM 在下一轮 react 里自己决定（再调、改 prompt、解释给用户、放弃）。**永不 abort run**
3. **只有编程错**（KeyError / TypeError 等 LLM proxy 解析错）才走 fatal `ErrorEvent` 路径 + abort run
4. **已发出去的 ToolStart 必须有 ToolEnd 收尾**（即便 LLM 错发生在 tool 之间）—— 通过 retry helper 把"已 emit 的 delta"状态化，retry 成功后从断点继续；retry 耗尽则 system note + 下轮 react 触发 LLM 重发 tool call，原 ToolStart 自然被新 ToolStart 覆盖或视为残缺（由 MonoDesk 端 onToolEnd call_id 匹配兜底，见 [fix-tool-end-on-interrupt.md](./fix-tool-end-on-interrupt.md)）

## 设计

### 1. 新增 `core/llm_proxy/retry.py`：retry_stream helper

把 LLM streaming 包成重试装饰器。第一次失败 → sleep 1s 重试；再失败 → sleep 2s；再失败 → sleep 4s；最后一次失败 → 抛最终异常给 caller（**不**自动重抛到外层 except — 见 §3 区分）。

```python
# core/llm_proxy/retry.py
from __future__ import annotations
import asyncio
import logging
from collections.abc import AsyncIterator

import httpx

from core.protocol import LlmChunk

_log = logging.getLogger("monox.llm_proxy.retry")

MAX_LLM_RETRIES = 3

# LLM 网络错 → retry。
# 不包括 ValueError / KeyError / TypeError（LLM proxy 解析 chunk 错，那不是网络问题）
_RETRY_EXCEPTIONS = (
    httpx.HTTPStatusError,    # HTTP 4xx/5xx（raise_for_status 触发）
    httpx.RequestError,       # 连接错、socket reset、TLS handshake
    httpx.TimeoutException,   # connect / read / pool timeout
    ConnectionError,          # 兜底（python builtin）
    OSError,                  # socket 资源耗尽
)


async def retry_stream(stream_factory, *args, **kwargs) -> AsyncIterator[LlmChunk]:
    """包 stream() 调用，失败重试 3 次（指数退避 1s/2s/4s）。

    stream_factory: async callable returning AsyncIterator[LlmChunk]
                    （通常是 lambda: llm.stream(messages, tools=..., options=...)）

    重试语义：每次 retry 复用同 messages（LLM 接收同 input）。但**已经 yield 出去的
    delta 不会重新 yield**（caller 已经 emit 给 output_queue / frontend）—— caller
    需要自己处理"重试断点"（见 §2）。
    """
    last_exc: Exception | None = None
    for attempt in range(1, MAX_LLM_RETRIES + 1):
        try:
            async for chunk in stream_factory(*args, **kwargs):
                yield chunk
            return  # 整个 stream 正常结束
        except _RETRY_EXCEPTIONS as exc:
            last_exc = exc
            if attempt >= MAX_LLM_RETRIES:
                break
            backoff = 2 ** (attempt - 1)  # 1s, 2s, 4s
            _log.warning(
                "llm stream attempt %d/%d failed: %s — retry in %ds",
                attempt, MAX_LLM_RETRIES, type(exc).__name__, backoff,
            )
            await asyncio.sleep(backoff)
            continue
    # 重试耗尽 — 抛最后一次错（让 engine 走 system note 路径，不 abort run）
    assert last_exc is not None
    _log.error("llm stream failed after %d attempts: %s", MAX_LLM_RETRIES, last_exc)
    raise last_exc
```

### 2. `core/loop/engine.py:_react` LLM streaming 段改造

**line 445-497** 改造：

```python
try:
    opts = {"model_provider": self._model_provider} if self._model_provider else None
    from core.llm_proxy.retry import retry_stream

    stream_iter = retry_stream(
        lambda: self._llm.stream(messages, tools=tool_schemas, options=opts),
    )
    async for chunk in stream_iter:
        # C3：流式消费循环内协作检查中断——毫秒级响应，不依赖外部 cancel。
        if not interrupt_queue.empty():
            interrupt_queue.get_nowait()
            return _INTERRUPTED
        if chunk.delta_text:
            full_text += chunk.delta_text
            await output_queue.put(TokenChunk(text=chunk.delta_text))
        if chunk.delta_reasoning:
            reasoning_text += chunk.delta_reasoning
            await output_queue.put(ReasoningChunk(text=chunk.delta_reasoning))
        if chunk.delta_tool_calls:
            _merge_tool_calls(tool_calls, chunk.delta_tool_calls)
            for d in chunk.delta_tool_calls:
                idx = d.get("index", 0)
                if idx >= len(tool_calls):
                    continue
                entry = tool_calls[idx]
                cid = entry.get("id", "")
                if cid and cid not in pending_emitted:
                    pending_emitted.add(cid)
                    name = entry["function"].get("name", "")
                    await output_queue.put(ToolPending(
                        call_id=cid,
                        name=name,
                        tool_index=idx,
                        args_so_far=entry["function"].get("arguments", "") or "",
                    ))
        if chunk.finish_reason:
            finish_reason = chunk.finish_reason
        if chunk.usage:
            usage = chunk.usage
except _RETRY_EXCEPTIONS as exc:
    # 重试 3 次仍然失败 → 把错当 system note 反馈进 messages，
    # 让 LLM 在下一轮 react 自己决策（不再调、改 prompt、解释给用户、放弃）。
    # **不** abort run，**不**发 fatal ErrorEvent。
    _log.warning("llm stream failed permanently: %s — appending system note for self-recovery", exc)
    self._messages.append({
        "role": "user",
        "content": (
            f"[system note] LLM call failed 3 times: {type(exc).__name__}: {exc}. "
            f"The provider is temporarily unreachable. Try a different approach "
            f"(simpler request / shorter prompt / different tool sequence), or "
            f"explain the situation to the user honestly."
        ),
    })
    # 标记当前 react step 已"完成"（虽然没拿到 LLM 输出），让 run() 主循环继续走下一轮
    return ""  # empty assistant message; 下一轮 react 会看到 system note
except Exception as exc:
    # 真正编程错（LLM proxy 解析 chunk 错 / KeyError / TypeError 等）→ fatal 兜底
    if self._traces is not None and self._current_turn_id is not None:
        await self._traces.record_llm_span(
            self._current_turn_id,
            model=_llm_model(self._llm),
            messages=messages,
            response_text=full_text,
            reasoning_content=reasoning_text or None,
            usage=usage,
            finish_reason=finish_reason,
            latency_ms=int((time.monotonic() - t0) * 1000),
            status="error",
        )
    raise  # 让外层 except 走 fatal ErrorEvent + finalize_aborted
```

要点：

- **`_RETRY_EXCEPTIONS` 在 `Exception` 之前匹配** —— 重试耗尽的 network 错**不**走 fatal 路径
- **不补 ToolEnd** —— 已 emit 的 ToolStart 会卡 running。但 retry 耗尽时 LLM 没拿到 finish_reason，tool_calls 也不会被 dispatch 给 tool 执行；下一轮 react 看到 system note 后 LLM 会重新决策（也许再调同 tool、也许不调），自然补救。MonoDesk 端的 onToolEnd call_id 匹配兜底处理（见 `fix-tool-end-on-interrupt.md`）
- **`return ""` 不算异常** —— `_react` 的"完成"信号（empty assistant text），run() 主循环看到 return "" 不会走 finalize_aborted，messages 已经多了一条 system note，下一轮 react 自然接续

### 3. `core/loop/engine.py:run()` 外层 except 区分 fatal / recoverable

`line 247-258` 的 except 只剩 fatal programming error。retry 耗尽的 network 错被 §2 的 `return ""` 兜住，根本不会到达外层 except。

```python
# 旧：
except Exception as exc:
    _log.exception("react step failed: %r", exc)
    await output_queue.put(ErrorEvent(
        code="llm_error", msg=f"{type(exc).__name__}: {exc}", retryable=True,
    ))
    await finalize_aborted("error")
    self._step_task = None
    continue

# 新（保持 except Exception 兜底编程错，不变）：
except Exception as exc:
    # 真正的编程错（不是网络错）—— record trace + fatal ErrorEvent + abort run
    _log.exception("react step failed (fatal): %r", exc)
    await output_queue.put(ErrorEvent(
        code="internal_error",   # 改名为 internal_error（不再是 llm_error）
        msg=f"{type(exc).__name__}: {exc}",
        retryable=False,
    ))
    await finalize_aborted("error")
    self._step_task = None
    continue
```

要点：

- ErrorEvent code 从 `"llm_error"` 改为 `"internal_error"` —— 语义更准确（编程错，不是 LLM 错）
- **MonoDesk 端** ErrorBlock 仍按现有逻辑显示，但触发频率大幅降低（之前每次网关抖都炸，现在只有真的编程错才显示）

## 不变量

- **`ErrorEvent("llm_error")` 永远不再被发** —— engine.py 全局 grep 应该为空
- **`core/llm_proxy/retry.py` 是纯 helper**，不写 state，只 wrap stream() 调用
- **retry_stream 接受 lambda 工厂** —— 每次 retry 重新调 factory（避免 stream 迭代器复用导致状态污染）
- **重试复用同 messages** —— LLM 接收同 input；已 yield 给 caller 的 delta 不会重新 yield（caller 已经 emit 过）
- **不补 ToolEnd** —— retry 耗尽时由下一轮 react 自然补救；不引入"残缺 tool 状态机"
- **wire 协议零变化** —— ErrorEvent schema 不变；只是 engine 不再发 `llm_error`

## 不做的事

- 不引入 "exponential backoff with jitter" —— 当前 1s/2s/4s 已经够小，不需要 jitter（单 user session 没有 thundering herd）
- 不做"per-provider retry budget" —— 全 provider 用同 retry 策略
- 不动 wire 协议 —— ErrorEvent code 字段保留 `"internal_error"` 这个新值（已是 Literal[str]，加一个常量值即可）
- 不动 LLM streaming 段内部（C3 interrupt 协作检查保留）

## 验证

### 1. 新增 `tests/test_llm_retry.py`

- **retry 一次失败第二次成功** —— mock stream_factory：第一次抛 `httpx.HTTPStatusError`，第二次正常 yield 3 个 chunk。assert `retry_stream` 把第二次的 3 个 chunk 都 yield 出来；call_count == 2
- **retry 全部失败抛最终异常** —— mock 3 次全抛。assert `retry_stream` 最终抛最后一次异常，call_count == 3
- **不重试 ValueError** —— mock 抛 `ValueError`。assert `retry_stream` 立刻传播，不重试，call_count == 1
- **指数退避时间** —— mock 测 wall-clock：3 次失败 total sleep ≈ 1 + 2 + 0（最后一次不 sleep）= 3s ± 抖动
- **已有 chunks 不重新 yield** —— mock：第一次 yield 2 个 chunk 然后抛错；第二次正常 yield 3 个 chunk。caller 收到 total 5 个 chunk（2 旧 + 3 新），不是 3

### 2. 新增 `tests/test_llm_error_recovery.py`（engine 集成）

- **retry 耗尽 → system note 进 messages，run 不 abort** —— mock llm.stream 抛 `httpx.HTTPStatusError`，模拟整条 run（user_input → react → 异常 → 下一轮 react 看到 system note）。assert：messages 末尾有 `[system note] LLM call failed...`；**没有** emit `ErrorEvent("llm_error")`；`run()` 主循环没退出
- **retry 一次成功 → 用户无感** —— mock llm.stream：第一次抛错，第二次正常。assert：没 emit `ErrorEvent`；正常 emit `FinalMessage`；caller 收到所有 token
- **编程错（ValueError）走 fatal 路径** —— mock llm.stream 抛 `ValueError`。assert：emit `ErrorEvent("internal_error")`；`finalize_aborted` 被调用

### 3. 回归

- `tests/test_interrupt.py` 全部不破（interrupt 是用户主动打断，跟本 spec 无关）
- `tests/test_e2e.py` 4/4 通过
- `tests/test_attachments.py`（如果有）不破

### 4. 手工 e2e

```bash
cd MonoX
# 1) 配故意失败的 provider（base_url 改成不存在的域名）
#    重启 runtime，发一条消息
#    预期：MonoDesk 看到 retry 日志（dim note "retrying llm..."），最终失败 → agent 解释"网络不通"
#    UI 不再出现大红框 llm_error

# 2) 配正常 provider，发消息
#    预期：retry 不触发，正常对话
```

## 进度

- 设计：本文档
- 实现：未开始