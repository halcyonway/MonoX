# metric-context-window: MetricChunk 携带 context_window，前端算占比

> **功能类**。本 spec 只定义 wire 层契约和 MonoX 端实现，
> MonoDesk 端的 UI 渲染见 [MonoDesk/spec/requirements/trace-context-percent.md](../../../MonoDesk/spec/requirements/trace-context-percent.md)。

## 问题

MonoDesk 想在 trace 页面和 chat 顶部显示「当前 context 已用 X / Y，Z%」。

现状：

- `core/loop/metric.py:9` 的 `StepMetric` 只记录 prompt / completion / cached_tokens，**没有模型 context window**
- `core/config.py:ModelProvider`（line 41-49）也没有 context_window 字段
- `core/llm_proxy/openai_stream.py` 调用 API 时返回的 `usage` 也不带 context_window（OpenAI 不返回这个，得客户端自己知道）

MonoDesk 知道当前 session 用的 model 名（hello 帧 `model_provider`），
但**没有权威的 context window 数值**——不同模型变体 context 不同
（claude-sonnet-4-5 200K vs gpt-4o 128K vs deepseek-chat 64K），
不能写死在 client。

## 目标

1. wire 层 MetricChunk 携带 `tokens.context_window`（int），让 client 能算占比
2. 配置层 `ModelProvider.context_window` 可选字段，缺失时 MetricChunk 不带这个字段（client 用 fallback / 不显示）
3. 兼容性：老 client 不识别 `context_window` 字段不影响（forward-compatible）

## 设计

### 1. config.py：ModelProvider 加 context_window

```python
@dataclass(frozen=True)
class ModelProvider:
    model_real_name: str
    base_url: str
    apikey_env: str
    timeout: int = 60
    extra_params: dict[str, Any] = field(default_factory=dict)
    # 模型 context window（token 数）。None = 未知（client 拿到 MetricChunk
    # 时不显示占比或用 fallback 默认值）。
    context_window: int | None = None
```

config.toml 用法：

```toml
[[llm.providers]]
name = "main"
model_real_name = "claude-sonnet-4-5"
base_url = "https://api.anthropic.com/v1"
apikey_env = "ANTHROPIC_API_KEY"
context_window = 200000

[[llm.providers]]
name = "haiku"
model_real_name = "claude-haiku-4-5"
base_url = "https://api.anthropic.com/v1"
apikey_env = "ANTHROPIC_API_KEY"
context_window = 200000
```

### 2. LlmProxy：每次 stream 调用 resolve 真实 provider 的 context_window

`core/llm_proxy/proxy.py` 在创建 LLM 调用 client 时附带 context_window：

```python
@dataclass
class _ResolvedCall:
    base_url: str
    api_key: str
    model: str
    context_window: int | None  # 新增
    ...
```

具体改动位置：

- `core/llm_proxy/openai_stream.py`：`stream()` 函数签名加 `context_window: int | None = None`，
  返回的 `LlmChunk` 里 `usage` dict 注入 `"context_window": context_window`（仅当 not None）
- Anthropic stream / 其他 backend 同样处理

### 3. MetricChunk：tokens dict 加 context_window

`core/loop/metric.py:StepMetric.tokens` 是 `dict[str, Any]`（OpenAI usage 透传），
context_window 直接塞进 dict：

```python
# 原本
usage = {"prompt_tokens": 1200, "completion_tokens": 80, "cached_tokens": 800}
# 注入后
usage = {
    "prompt_tokens": 1200,
    "completion_tokens": 80,
    "cached_tokens": 800,
    "context_window": 200000,  # 来自 provider 配置
}
```

wire_frames 序列化时整个 dict 透传，无需改 schema（[wire_frames.py:217](../../../MonoX/core/protocol/wire_frames.py) 已有 `metrics: dict` 字段）。

### 4. client 算占比

`prompt_tokens / context_window * 100` —— 实时反映「上一轮 react step 实际发给 LLM 的 prompt 占模型 context 的比例」。

注意：

- 这是**上一个 step 的快照**，不是当前 session 的累加。session 多 step 时 prompt 大小会随 messages 增长。
- compression 触发后 prompt 会变小。
- MonoDesk 想做「当前 session 的实际 prompt 大小」也可以（= last step 的 prompt_tokens），
  比真正的「当前 session 实际输入 LLM 的字节数」已经够准。

## 不做的事

- 不做实时 prompt 累加 / session 级别的 token 账本（每 step metric 足以）
- 不把 context_window 也塞进 hello 帧（用户可中途切 provider，每 step 的 metric 才是真实上下文）
- 不在 prompt 超 context_window 时自动截断（已有 [context-compression.md](./context-compression.md) 处理）

## 验证

1. 单测：`tests/test_metric_context_window.py`
   - provider 有 context_window → MetricChunk.metrics.tokens.context_window = 200000
   - provider 没设 context_window → MetricChunk.metrics.tokens 不含这个 key
   - 切 provider（同一 session）→ context_window 跟着变
2. 集成测：Mock LlmProxy → 跑完整 react step → 断言 MetricChunk payload 含 context_window
3. 手动：MonoDesk trace 页面 + chat 顶部正确显示「X / Y，Z%」

## 进度

- 设计：本文档
- 实现：未开始