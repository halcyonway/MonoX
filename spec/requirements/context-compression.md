# context-compression: L1/L2/L3 上下文压缩

## 问题

多 turn 对话累积，context window 终将打爆。

## 设计

压缩逻辑集中到 `core/loop/compression.py` 的 `CompressionService`，LoopEngine 只在明确时机调用，不散落压缩代码。

压缩模型通过 `[llm.compression]` 独立配置（**必填**，缺失启动报错）；未填字段回退主 `[llm]`。摘要采样参数走 `[llm.compression.options]`，不再硬编码。

| 层 | 粒度 | 触发 | 实现 |
|---|---|---|---|
| **L1** | 单条 tool_result 截断 | stdout/stderr 超过阈值（默认 4000 字符） | 截断 + 存 `budget_id`，LLM 可调 `read_tool_result_budget` 拿完整版 |
| **L2** | 老 turn 折叠为 summary | 全量 messages 字符数超阈值（默认 24000）且 user turn 数超过保留数 | 调 LLM 把最早 N 轮汇总成一段 summary |
| **L3** | Memory.md 自动追加 | L2 成功产出 summary | `append_fact` 写入 `## Conversation Summaries` |

## CompressionService 提供什么

- `compress_tool_result(result)`：L1，同步，超阈值则返回 `truncated=True + budget_id` 的截断结果。
- `should_compress(messages)`：L2 判断，纯计算不调 LLM，供 engine 决定是否 emit `compressing`。
- `maybe_summarize(messages, session_key)`：L2，超阈值时折叠最早 N 轮，成功后在内部触发 L3。
- `maintain_memory(session_key, summary)`：L3，把 summary 追加到 Memory.md。

## LoopEngine 什么时候用

1. **L1**：`tool.execute` 之后、`format_tool_message` 之前，调用 `compress_tool_result`。
2. **L2/L3**：每个 react step 的 `assemble_messages` 之前，先 `should_compress`（emit 状态），再 `maybe_summarize`；`read_index` 必须放在 L2 之后，才能拿到刚写入 Memory 的 summary。

```python
# 每个 step，assemble 之前
if self._compression.should_compress(self._messages):
    await output_queue.put(StatusChange(state="compressing"))

self._messages = await self._compression.maybe_summarize(
    self._messages, self._session_key
)

memory_index = await self._memory.read_index(self._session_key)
messages = assemble_messages(...)

# tool dispatch 处，execute 之后
if name != ReadToolResultBudgetTool.name:
    result = self._compression.compress_tool_result(result)
```

## 设计要点

- L2 摘要只写入 Memory.md，不塞回 `self._messages`；summary 通过 `memory_index` 进入 system 的 `## Memory` 段。
- L2 字符估算用 `json.dumps`，不依赖流式 `usage`。
- turn 边界以 `role == "user"` 划分，保留最近 `l2_keep_turns`（默认 2）个完整 turn。
- `read_tool_result_budget` 自身结果跳过 L1，避免读取又截断的循环。
- L2 摘要失败/为空时降级为不折叠，主 loop 不崩。

## 验证

- L1：长 stdout/stderr 截断 + `budget_id` 可读回完整结果。
- L2：超阈值折叠最早 N 轮，summary 写入 Memory.md；低于阈值不触发。
- L3：`maintain_memory` 追加 `## Conversation Summaries`。
- e2e：bash 输出 5000 字符被 L1 截断；多 turn 触发 L2。

## 风险

- budget 是内存 dict，重启后旧 `budget_id` 失效（本次会话临时句柄）。
- L2 summary 质量取决于 LLM，失败降级为不折叠，保证主 loop 不崩。
- Memory.md 的 summary 区域会线性增长，v1 先接受，未来再加合并/上限。
