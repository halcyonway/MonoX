# context-compression: L0/L1/L2 上下文压缩

## 问题

多 turn 对话累积，context window 终将打爆。

## 现状（v0.9）

- `core/loop/context.py` 已经有 `compress_tool_result()` 工具方法（占位）
- engine.py 里有 `TODO(v2): L1 压缩` 注释
- 没启用

## 设计

三档压缩（行业惯例）：

| 档 | 粒度 | 触发 | 实现 |
|---|---|---|---|
| **L0** | 不压缩 | 始终 | 原样 |
| **L1** | 单条 tool_result 截断 | tool_result 字节超过阈值 | 截断 + 留 `budget_id` 标 + budget_tool 调 LLM 拿完整版 |
| **L2** | 老 turn 汇总 | context 超过总阈值（如 80% window） | 把最早 N 轮的 user/assistant 调 LLM summarize，存 Memory.md 或 L2 buffer，新一轮 system 注入「Earlier summary: ...」 |
| **L3**（可选） | L2 + Memory.md 自动维护 | L2 多次触发 | engine 周期性提「请把以下对话维护到 Memory.md」 |

## L1 设计

- `core/loop/context.compress_tool_result(result, budget_tool, threshold=2000) -> tuple[truncated, full]`
- truncated 内容用 `[truncated: 2000 chars; call read_tool_result_budget(budget_id="x") for full]`
- 完整内容存 `budget_tool`（已有 `ReadToolResultBudgetTool` 工具）
- engine 在 `_handle_tool` 处调 `compress_tool_result` 而非直接用 raw

## L2 设计

- 累积所有 turn 的 `total_tokens`
- 超阈值时：取最早 N turn，build prompt 给 LLM 生成 summary
- 替换 messages 里最早 N turn → 一个 `{role: "system", content: "[earlier summary]\n..."}`
- summary 内容同步追加到 `Memory.md`（可选）

## L3 设计

- 在 L2 触发同时，让 LLM 维护 Memory.md（新增事实 / 删除过期）
- 需要 MemoryStore 暴露 `append_fact()` 接口

## 触发点

engine 改造：
```python
async for chunk in self._llm.stream(messages, tools=...):
    ...

# step 末尾
if total_tokens > THRESHOLD:
    messages = await self._maybe_l2_compress(messages)

# tool dispatch 前
result = compress_tool_result(result, self._budget_tool)
```

## 验证

- L1：mock tool 输出 5KB → 截断到 2KB + budget_id → LLM 收到 truncated，可调 budget 拿回
- L2：跑 10 turn 后 L2 触发，summary 正确覆盖最早 5 turn
- e2e：保持 4/4 不回归 + 新增 1 个 L1 + 1 个 L2 测试

## 风险

- L1 截断后 LLM 行为可能变（特别是「我说错了，看 stdout」这种）
- L2 summary 质量取决于 LLM，可能丢关键事实 → Memory.md 兜底
- 压缩本身消耗 token（summary 调 LLM），可能 recursion 风险

## 进度

- 设计：本文档
- L0：已在用（不压缩）
- L1 / L2 / L3：未实现