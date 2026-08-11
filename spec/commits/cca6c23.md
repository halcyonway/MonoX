# cca6c23 — fix(v0.5): use reasoning_split API param (no ThinkFilter hack)

**问题**：MiniMax 等 provider 把 reasoning 嵌在 content 里（`<think>...</think>` 标签），之前用正则过滤后丢，但 LLM 后续引用 reasoning 就拿不到了。

**修**：

- 调 provider API 时加 `reasoning_split: true`
- provider 在 stream 时区分 `delta.reasoning_content` vs `delta.content` 两个字段
- engine 分两个 chunk 上行：`ReasoningChunk` / `TokenChunk`

**好处**：reasoning 不污染 final content，且可单独可视化（v0.8 才暴露给用户）。

不再做字符串过滤 / ThinkFilter hack — 不动 provider 时也避免 parsing fragile。