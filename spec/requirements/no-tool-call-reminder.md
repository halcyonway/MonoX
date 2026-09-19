# no-tool-call-reminder: reasoning-only turn 不让 run 直接结束

> **Bug 类**。LLM 偶发把 tool call 漏到 reasoning content 里、或者干脆没调 tool
> 也没出 final text 时，run 直接 finalize + wait_io，UI 永远 thinking 之后
> 空荡荡。本文定义：**检测到「reasoning 非空 ∧ content 空 ∧ tool_call 空」时**，
> 给 LLM 注入一条 reminder system note 让它重出 tool call（或 content），
> **最多 3 次**；超过放过去让正常 final 路径走。

## 问题

`core/loop/engine.py:_react()` line 671 的 final-message 分支判定：

```python
if not tool_calls or finish_reason == "stop":
    assistant_msg = {"role": "assistant", "content": full_text}
    self._messages.append(assistant_msg)
    ...
    final_text = full_text
    ...
    await output_queue.put(StatusChange(state="wait_io"))
    return final_text
```

`finish_reason == "stop"` 且 `tool_calls` 为空时，**不管 `full_text` / `reasoning_text`
有没有内容**都直接 `return final_text`（空字符串）。MonoDesk 收到的 final
message 是空字符串 → 看不到文本、看不到 tool 卡、永远 thinking→idle。

上游案例：minimax 网关把 tool_call JSON 序列化进了 `delta_reasoning`，stream
结束后 `finish_reason=stop`、`delta_tool_calls` 全空、`delta_text` 空，只剩
`reasoning_text` 非空。这是 API 侧的 bug，但 abort run 把锅丢给用户太粗暴。

## 目标

1. **检测异常输出**：`reasoning_text` 非空 ∧ `full_text` 空 ∧ `tool_calls` 空 ∧ `finish_reason == "stop"` ⇒ reminder
2. **注入 reminder**：往 `self._messages` 追加一条 `{"role": "user", "content": "..."}`
   system note（跟 [llm-error-recovery.md](./llm-error-recovery.md) §3 的 system
   note 同形态），让下一轮 react 的 LLM 看到并重出 tool call（或 final text）
3. **最多 3 次**：`self._no_tool_reminder_count` 计数器；≥ 3 时放过去，让
   现有 final-message 路径正常走（输出空 final，让 UI 至少进 idle）
4. **不动其他东西**：proxy 不动 / wait_io fallback 不加 / final-message 分支
   不改 / tool dispatch 不改

## 设计

### 1. 触发条件（严格三联判定）

在 `_react()` 里 record_llm_span 成功路径之后（line 669 附近），final-message
判定之前（line 672 之前）插入 if 块：

```python
# no-tool-call reminder：检测 reasoning-only turn
no_tool_case = (
    bool(reasoning_text)
    and not full_text
    and not tool_calls
    and finish_reason == "stop"
)
if no_tool_case:
    self._no_tool_reminder_count += 1
    _log.warning(
        "no-tool-call reminder triggered step=%d count=%d reasoning_len=%d",
        self._step_idx,
        self._no_tool_reminder_count,
        len(reasoning_text),
    )
    # 注入 reminder 后让本 step 走完（continue 进入下一轮 react），
    # 但**不** append assistant_msg、不 finalize final_text、不发 wait_io。
    # LLM 下一轮看到 reminder 后会重出 tool call 或 final text。
    if self._no_tool_reminder_count <= 3:
        self._messages.append({
            "role": "user",
            "content": (
                "[system note] Your previous turn returned only reasoning content "
                "with no tool call and no final answer text. "
                "The reasoning appears to contain a tool call that was not emitted "
                "as a structured tool_calls block. "
                "Please re-emit the tool call as a proper structured tool_calls "
                "in your next response, OR write the final answer as plain text."
            ),
        })
        # 不写 checkpoint（跟 llm-error-recovery §3 同款 system note 一样不持久化）。
        continue
    # count > 3：放过去，让 final-message 路径正常输出空 final + wait_io。
    # _log.warning 已经记录，不需要再 emit。
```

### 2. 计数器位置

`self._no_tool_reminder_count: int = 0` 加在 `__init__` 里（跟
`self._step_idx`、`self._session_metric` 同类 session 级别的可变状态）。

session 销毁时随 engine 一起被回收，不需要额外清理。

### 3. 不变量

- **不动 proxy**：proxy 只负责 yield chunk，不感知 reminder 逻辑
- **不动 wait_io fallback**：超过 3 次直接走现有 final-message 分支，**不**主动发 wait_io 工具调用
- **不动 final-message 分支**：line 672 之后的代码原样；reminder 走 continue 让下一轮 react 重跑 LLM
- **不动 tool dispatch**：tool_calls 非空时直接走 line 701 的 tool 分支，不进 reminder if
- **不动 LLM 网络错路径**：line 591-599 的 system note 跟 reminder 是不同触发条件，互不影响
- **不动 checkpoint 持久化**：reminder 跟 llm-error-recovery 的 system note 一样不写 checkpoint
  （避免污染 replay；session 重启后 reminder 计数归零，但概率可忽略）
- **不动 MetricChunk**：本 step 的 latency / ttft / tokens / usage 已经在 record_llm_span
  前发出去了（line 633），reminder 不会重复 emit；tool_calls_count 保持 0（确实没 tool call）

### 4. 与现有 final-message 分支的衔接

| 情况 | 走哪 |
|---|---|
| `tool_calls` 非空 | line 701 tool dispatch（原样） |
| `tool_calls` 空 ∧ `full_text` 非空 | line 671 final-message（原样） |
| `tool_calls` 空 ∧ `full_text` 空 ∧ `reasoning_text` 空 ∧ `finish_reason == "stop"` | line 671 final-message（原样；UI 看到空 final，但至少进 wait_io） |
| `tool_calls` 空 ∧ `full_text` 空 ∧ `reasoning_text` 非空 ∧ `finish_reason == "stop"` ∧ count ≤ 3 | **新 reminder 分支**（continue 下一轮） |
| `tool_calls` 空 ∧ `full_text` 空 ∧ `reasoning_text` 非空 ∧ `finish_reason == "stop"` ∧ count > 3 | line 671 final-message（放过去，输出空 final） |

第 4 行是本 spec 新加的；其他行都是原状。

### 5. LLM 收到 reminder 后的预期行为

reminder 内容明确告诉 LLM「你的 reasoning 里看着像有 tool call 没结构化 emit」，
引导它下一轮：

- 优先：重新结构化 emit tool_call（如果之前确实漏到 reasoning 里了）
- 退路：直接写 final answer 文本（如果它本就想纯对话）

如果 LLM 仍然 reasoning-only：count += 1，下一轮再 reminder，最多 3 次。
超过 3 次的兜底：让现有 final-message 分支输出空 final + wait_io，UI 进 idle，
**不**让用户看到无限 thinking。

### 6. 为什么不让 reminder 走 `role=assistant` 自我 message？

跟 [llm-error-recovery.md](./llm-error-recovery.md) §3 保持一致（那条 system
note 用 `role=user`），统一一种注入形态便于后续 grep / 监控。如果未来要改
两种 note 的注入方式，一次性改完。

### 7. 为什么不复用 `retry_stream`？

`retry_stream` 是网络层重试（HTTP 4xx/5xx、连接错），跟本 spec 的「输出
内容异常」是不同的失败模式。reminder 不在 proxy 层做，是 engine 层拿到
chunk 聚合之后判定输出三元组形态失败。本 spec 跟 retry_stream 互不影响。

## 测试

### `tests/test_engine_no_tool_reminder.py`（新文件）

覆盖以下 case：

| Case | 期望 |
|---|---|
| 1. 正常 tool_call（tool_calls 非空） | 不触发 reminder；count = 0 |
| 2. 正常 final text（full_text 非空） | 不触发 reminder；count = 0 |
| 3. 正常空 final（content 空 ∧ reasoning 空 ∧ tool_call 空） | 不触发 reminder；走原 final-message 路径 |
| 4. reasoning-only 第 1 次 | 触发 reminder；count = 1；messages 多 1 条 system note；不走 final-message；继续下一轮 |
| 5. reasoning-only 第 2 次 | count = 2 |
| 6. reasoning-only 第 3 次 | count = 3 |
| 7. reasoning-only 第 4 次（count 已到 3） | **不**触发 reminder；走 final-message；count 保持 3 |
| 8. 计数器跨 step 累加（同 session 多轮 reasoning-only） | count 累加正确 |
| 9. finish_reason 不是 "stop"（比如 "length" / "tool_calls"） | 即使 reasoning-only 也不触发 reminder |
| 10. reminder 注入后 checkpoint 不追加 | 验证 `self._checkpoint.append` 没被多调 |

### 不变量测试

- 不动 proxy：原有 `tests/test_llm_proxy_retry.py` 全过
- 不动 final-message：原有 reasoning-only 之前的所有 engine 测试全过
- 不动 tool dispatch：原有 tool 调用测试全过
- 不动 interrupt / cancel 路径：原有 interrupt 测试全过

## 验证

```bash
cd MonoX && uv run pytest tests/ -q
# 期望：新增 test_engine_no_tool_reminder.py 全过
# 现有 ~150 测试全过（除已知的 test_multimodal_understand_path 8 个旧 bug）
```

### 手工 e2e（等用户配合）

1. 复现 case：构造 prompt 让 minimax 网关返回 reasoning-only turn
2. 观察 trace：第一轮 reasoning-only → reminder system note → 第二轮正常 tool call
3. 观察 UI：thinking 状态保持连续；不出现「idle 后突然 reasoning-only 再 thinking」的撕裂

## 不动

- proxy / retry_stream
- wait_io 工具实现
- final-message 分支（line 671 之后的所有代码）
- tool dispatch 分支（line 701 之后）
- LLM 网络错的 system note 路径
- 任何 channel / UI / LLM SDK import
