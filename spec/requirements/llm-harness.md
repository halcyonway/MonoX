# llm-harness: LLMProxy retry / fallback / rate-limit

## 问题

`core/llm_proxy/openai_stream.py`（v0）只是裸 httpx 调用：

- 网络抖动 → 整 turn 失败
- 5xx 短暂故障 → 不重试
- 单 provider → provider 挂了全 runtime 挂
- 用户超 rate limit → 不排队 / 不换 provider
- 长时间运行不知道预算（token 用了多少 / 还剩多少）

## 现状（v0.9）

- 单模型 + 简单调用，无 retry / fallback
- 模型特定参数（如 MiniMax 的 `reasoning_split`）通过 `cfg.extra_params` 透传

## 设计

### 三件事：retry / fallback / rate-limit

#### 1. Retry

针对 transient failure（5xx / 408 / 网络错误）：

```python
@async_retry(
    max_attempts=3,
    backoff=ExponentialBackoff(base=1.0, max=10.0),
    retry_on=(httpx.HTTPStatusError, httpx.NetworkError, json.JSONDecodeError),
    retry_if=lambda e: 500 <= e.response.status_code < 600 or e.response.status_code == 408,
)
async def stream(self, ...):
    ...
```

- 仅 retry **建立连接 / 首个 chunk 之前**；流已开始则不 retry（避免吐半个回复）
- 4xx（除 408 / 429）不 retry —— client error，重试无意义
- 3 次后失败 → 抛 `LLMUnavailable`，让 engine 决定走 fallback

#### 2. Fallback（多 provider）

配置：

```toml
[[llm.providers]]
name = "primary"
api_base = "https://api.example.com/v1"
api_key = "..."
model = "gpt-4"

[[llm.providers]]
name = "fallback"
api_base = "https://api.another.com/v1"
api_key = "..."
model = "claude-3-5-sonnet"
```

Proxy 在 primary 抛 `LLMUnavailable`（retry 用尽）时换 fallback：

```python
class FallbackProxy(LLMProxy):
    async def stream(self, ...):
        for provider in self._providers:
            try:
                async for chunk in provider.stream(...):
                    yield chunk
                return
            except LLMUnavailable:
                logger.warning(f"provider {provider.name} unavailable, trying next")
        raise LLMUnavailable("all providers failed")
```

- provider 间**不**并行（同 prompt 跑两次浪费钱 / 双回复混乱）
- fallback 时记录 metric：哪个 provider 救场
- 实际仅 fallback on retry-exhausted，不 fallback on 4xx（业务问题）

#### 3. Rate-limit / Token 预算

两件事：

**Rate limit（per provider）**：
- 简单 token bucket，挂在 proxy 外层
- 配置：`[llm.primary] rate_limit_rpm = 60`（requests/min）
- 超 limit → `await asyncio.sleep(retry_after)` 再发

**Token 预算（全局）**：
- 累加每 turn 的 `prompt_tokens + completion_tokens`（来自 LLM 返回的 usage 字段）
- 配置：`[llm] daily_token_budget = 1000000`
- 超预算 → 抛 `LLMBudgetExceeded`，gateway / engine 给用户提示
- 注意：streaming 模式下 usage 通常在最后一个 chunk 才有；得攒到结束才知道

```python
class TokenBudget:
    def __init__(self, daily: int): self._limit = daily; self._used = 0
    def charge(self, n: int):
        if self._used + n > self._limit: raise LLMBudgetExceeded
        self._used += n
```

### 与现状集成

`core/llm_proxy/openai_stream.py` 拆成两层：

```
LLMProxy (Protocol)
    │
    ▼
HarnessProxy          # retry + fallback + rate-limit
    │
    ├── ProviderProxy (primary)
    ├── ProviderProxy (fallback)
    └── TokenBudget (charge on each completed turn)
```

每层独立可测。

### Metric

新增 metric（已有 `core/loop/metric.py` 框架）：

- `llm.retry.count{provider, reason}` —— 重试次数
- `llm.fallback.count{from, to}` —— fallback 触发次数
- `llm.tokens.used{provider}` —— 当日已用
- `llm.tokens.remaining` —— 剩余预算

debug 模式 / spec metrics 输出。

## 验证

- 单测：retry 注入 mock 5xx 序列 → 第 N 次成功；超 max_attempts → 抛
- 单测：fallback 注入 primary raise → fallback 接住 + 正常 yield
- 单测：rate-limit 超 rpm → 实际等待时间符合预期
- 单测：token budget 超限 → 抛 LLMBudgetExceeded
- 集成测：用 primary + fallback 都 mock，验证 fallback 路径
- 回归：原有 e2e 4/4 不破

## 风险

- retry + fallback 叠加 → 出错时 lag 长（base=1s → 2s → 4s + fallback 重试）→ 设 max_attempts=2 兜底
- 流中断时已发出部分 token → 用户看到一半回复；Engine 把已收 chunk 当 final 提示「fallback 后重发」
- token budget 累加跨进程不持久 → v1 仅 in-memory，重启重置；持久化留 v2
- 多 provider cost 不一 → metric 透明，让用户看

## 进度

- 设计：本文档
- 实现：未开始（中期目标）