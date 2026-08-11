# 56aceeb — docs: add third-party LLM provider examples to config.example.toml

扩充 `config.example.toml`，加入多个 OpenAI-compatible provider 示例（注释形式）：

- OpenAI 官方
- Anthropic（via proxy）
- DeepSeek
- 智谱 GLM
- 月之暗面 Kimi

每个示例注明 `api_base` / `model` 命名约定。后续 commit 加 MiniMax（`reasoning_split` 参数特殊）。