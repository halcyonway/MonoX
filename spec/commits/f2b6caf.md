# f2b6caf — feat(v0.8): debug mode shows loop internals (reasoning/state/metrics)

新增 `--debug` flag（之前用 `MONOX_DEBUG=1` env var，v0.8.1 移除）：

debug mode 多打：

- **ReasoningChunk**：LLM 思考过程（灰色 italic）
- **状态变更**：`StatusChange(state)` 行内显示（`wait_io` / `react` / `thinking`）
- **Metric 行**：每 turn `tokens / latency / step count`
- **tool dispatch args**：tool 调用的完整参数（正常 mode 只显示 tool name）

启动方式：`uv run python run.py --debug` 或 `--debug --session_key xxx`。

v0.8.1 把 reasoning 拆出来归为「基本 output」（即正常 mode 也显示），debug 只加 loop 内部细节 — 见 `c98d362`。