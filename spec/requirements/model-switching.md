# 模型切换技术方案

> **UI Demo**：MonoDesk 侧 ModelSwitcher 组件视觉稿：[`MonoDesk/spec/ui/model-switcher.html`](MonoDesk/spec/ui/model-switcher.html)

## 1. 核心理解

- **切换模型 = 请求里带 provider 名**：LlmProxy.stream() 的 options 带 model_provider，LlmProxy 自己查配置解析真实连接
- **全程透传**：Channel 指定 → RuntimeServer → SessionManager → LoopEngine → LlmProxy，中间不解析、不感知
- **per-request 而非 per-session**：不需要新建 session，切模型就在下一个请求的 options 里带 provider 名
- **协议暴露 provider 列表**：RuntimeServer 通过 hello 帧告知 MonoDesk 所有可用 provider 名

---

## 2. 配置结构

### `ModelProvider` dataclass

```python
@dataclass(frozen=True)
class ModelProvider:
    model_real_name: str   # 送给厂家的 model 字段（如 "gpt-4o"）
    base_url: str          # 如 "https://api.openai.com/v1"
    apikey_env: str        # 环境变量名，如 "OPENAI_API_KEY"
    timeout: int = 60
    extra_params: dict[str, Any] = field(default_factory=dict)
```

### `LLMConfig` 新增字段

```python
@dataclass(frozen=True)
class LLMConfig:
    # ... 现有字段 ...
    provider_name: str | None = None  # 默认 provider 名（引用 providers 字典的 key）
```

### `Config` 新增字段

```python
@dataclass(frozen=True)
class Config:
    # ... 现有字段 ...
    providers: dict[str, ModelProvider] = field(default_factory=dict)
```

### config.toml 示例

```toml
[llm]
model = "gpt-4o-mini"
provider_name = "openai"

[providers.openai]
model_real_name = "gpt-4o-mini"
base_url = "https://api.openai.com/v1"
apikey_env = "OPENAI_API_KEY"
timeout = 60

[providers.claude]
model_real_name = "claude-sonnet-4-20250514"
base_url = "https://api.anthropic.com/v1"
apikey_env = "ANTHROPIC_API_KEY"
timeout = 120
extra_params = { "thinking_max_tokens": 16000 }
```

---

## 3. Wire 协议

### 3.1 出站 hello（RuntimeServer → MonoDesk）

**首次连接时告知可用 provider 列表**：

```json
{
  "v": 1,
  "type": "hello",
  "seq": 0,
  "ts": 1234567890,
  "data": {
    "session_key": "default",
    "model": "gpt-4o-mini",
    "providers": ["openai", "claude"]
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `model` | string | 当前默认模型名（用于显示） |
| `providers` | string[] | 所有可用 provider 名列表（用于 MonoDesk 下拉框） |

### 3.2 入站 user_input（MonoDesk → RuntimeServer）

**用户切换模型后，下一个请求的 meta 里带 model_provider**：

```json
{
  "v": 1,
  "type": "user_input",
  "seq": 0,
  "ts": 1234567890,
  "data": {
    "session_key": "default",
    "text": "...",
    "meta": {
      "model_provider": "claude"
    }
  }
}
```

> 切换模型不需要新建 session，直接在下一个请求里带 `model_provider` 即可。

### 3.3 InboundEvent 新增 meta 字段

```python
@dataclass(frozen=True)
class InboundEvent:
    # ... 现有字段 ...
    meta: dict[str, Any] = field(default_factory=dict)  # 新增，支持 model_provider
```

---

## 4. 数据流

```
用户选择 provider "claude"
    ↓
MonoDesk 发 user_input { meta: { model_provider: "claude" } }
    ↓
RuntimeServer._recv_loop → from_frame → InboundEvent(meta={"model_provider": "claude"})
    ↓
SessionManager.dispatch_inbound → 路由到对应 session
    ↓
LoopEngine._react → user_input_event_xml 把 meta 携带进去
    ↓
LlmProxy.stream(messages, tools, options={"model_provider": "claude"})
    ↓
LlmProxy._resolve("claude") → 查 cfg.providers["claude"] → 得真实 base_url / api_key / model
    ↓
发请求到 claude 的 API
```

---

## 5. LlmProxy 改动

```python
class LlmProxy(LLMProxyProto):
    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg

    def _resolve(self, provider_name: str | None) -> tuple[str, str, str, int, dict]:
        """解析真实 base_url / api_key / model_real_name / timeout / extra_params。"""
        if provider_name and self._cfg.providers and provider_name in self._cfg.providers:
            p = self._cfg.providers[provider_name]
            api_key = os.environ.get(p.apikey_env, "")
            return p.base_url, api_key, p.model_real_name, p.timeout, p.extra_params
        # 回退到默认
        return self._cfg.api_base, self._cfg.api_key, self._cfg.model, self._cfg.timeout, {}

    async def stream(self, messages, tools=None, options=None):
        prov_name = (options or {}).get("model_provider")
        base_url, api_key, model, timeout, extra_params = self._resolve(prov_name)

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        payload.update(self._cfg.extra_params)
        payload.update(extra_params)
        payload.update(self._cfg.options)
        if options:
            payload.update(options)

        client = httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        # ... 发送请求 ...
```

> **关键**：options 里的 model_provider 只被 LlmProxy 读，其他层全程透传。

---

## 6. 各层职责

| 层 | 职责 |
|---|---|
| `config.toml` | 存 providers 配置 |
| `Config` | 加载配置，providers 放在 LLMConfig 里 |
| `LlmProxy` | 唯一能读 providers 的模块，根据 model_provider 解析真实连接 |
| `RuntimeServer` | 转发 InboundEvent，**不解析 meta** |
| `SessionManager` | 转发 InboundEvent，**不解析 meta** |
| `LoopEngine` | 把 meta 透传给 LlmProxy.stream()，**不解析** |
| `Channel` | 指定 model_provider（可选），**不感知 providers** |

---

## 7. 不支持切换的 channel（terminal / feishu / textual）

- 发 user_input 不带 `meta.model_provider`
- LlmProxy 用 config 默认的 `llm.provider_name`
- **零感知，零改动**

---

## 8. 文件变更清单

### MonoX 侧

| 文件 | 变更 |
|---|---|
| `core/config.py` | 加 `ModelProvider` + `LLMConfig.provider_name` + `Config.providers` |
| `core/llm_proxy/proxy.py` | `_resolve()` 从 `cfg.providers` 读 |
| `core/protocol/wire_frames.py` | hello 帧 data 加 `model` + `providers`；user_input 帧透传 meta |
| `core/runtime_server.py` | 发 hello 帧（含 model + providers） |
| `core/session_manager.py` | dispatch_inbound 透传 meta |
| `core/loop/engine.py` | 把 meta 透传给 LlmProxy.stream() options |
| `run.py` | providers 加载到 Config |

### MonoDesk 侧

| 文件 | 变更 |
|---|---|
| `src/ws/protocol.ts` | hello data 加 `model` + `providers`；InboundMessage data.meta |
| `src/ws/client.ts` | 解析 hello 的 providers 列表；发送时支持 meta |
| `src/store/sessions.ts` | session 状态加 `availableProviders` |
| `src/components/Chrome.tsx` | 加 ModelSwitcher 下拉框 |

---

## 9. 向后兼容

| 场景 | 行为 |
|---|---|
| config.toml 无 providers | LlmProxy 回退到 `cfg.api_base/api_key/model` |
| user_input 无 meta.model_provider | LlmProxy 用默认 provider_name |
| 旧 MonoDesk 连新 Runtime | 不解析 hello.providers，下拉框不显示（不影响使用） |
| 新 MonoDesk 连旧 Runtime | RuntimeServer 不发 hello，MonoDesk 等待超时后继续 |
