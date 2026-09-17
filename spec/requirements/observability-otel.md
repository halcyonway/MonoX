# observability-otel: Trace 系统切到 OTel 语义约定 + Span 嵌套 + TTFT 采集

> **重构类**。MonoX 当前 trace 系统的属性命名是 ad-hoc snake_case（`model` /
> `messages` / `tool_name` 等），跟 OpenTelemetry Semantic Conventions 不对齐；
> Span 层级扁平（Run → Turn → 扁平 Spans），没有 bootstrap / loop / finalize
> 三段式也没有 tool → act 的嵌套；TTFT 没采集；Reasoning span 不带 tool_specs。
> 本 spec 是 server 端契约文档，对应 client 端
> [MonoDesk/spec/requirements/trace-tree-redesign.md](../../../MonoDesk/spec/requirements/trace-tree-redesign.md)。
>
> 参考：
> - [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
> - [OpenTelemetry Span Kind](https://opentelemetry.io/docs/concepts/signals/traces/#span-kind)
> - Langfuse / Arize / Phoenix 的 trace tree 视觉惯例（AgentLoopRun → phase → turn → work）

## 问题

1. **属性命名是 ad-hoc snake_case**，没对齐 OTel Semantic Conventions：
   - 没法对接外部 OTel collector / 第三方 trace 工具
   - 新成员没法靠「这个 key 是 OTel 标准还是 MonoX 自创」快速理解
2. **TTFT 没采集**。`core/llm_proxy/proxy.py` 没在首个 chunk 打点；
   MonoDesk 客户端的 TTFT（`engine.ts:493` 的 `firstTokenAt - userSendTime`）是
   client-side 估算，含网络 + WebSocket 序列化 latency，不能准确反映「LLM 真正吐
   第一个 token 花了多久」。
4. **Span 层级扁平**（Run → Turn → 扁平 Spans，parent_id 全指向 turn_id，
   没真正嵌套）。跟 Langfuse / Arize / Phoenix 的视觉习惯不一致，看 trace 时
   不知道「bootstrap / loop / finalize 是哪个 span」。
5. **Reasoning span 不带 tool specs**。`engine.py:444` 构建 `tool_schemas`
   传给 LLM，但 trace 里没有「这次 LLM 被允许调哪些 tool」的记录。
6. **`tool.result` 不带 artifacts**。Wire-level `ToolEnd` 帧带 artifacts，但
   `record_act_span` 只取 stdout/stderr/exit_code，binary attachment 丢。

## 目标

1. **属性命名全量切 OTel** —— `gen_ai.*` / `tool.*` / `service.*` / `error.*` 等
   标准命名空间；agent 特有字段用 `loop.*` 自定义命名空间。OTel 没对应字段时
   按命名风格扩展（仍沿用 `前缀.子名` 形式）。
2. **SpanKind 扩到 8 个**，建立真正的树形结构：run → bootstrap / loop /
   finalize → turn → reason / act / compress → tool
3. **TTFT 采集**：`core/llm_proxy/proxy.py` 首个非空 delta 时打点，
   落到 `LlmChunk.first_chunk_at_ms`；engine 收后存到 `StepMetric.ttft_ms`，
   `record_llm_span` 时塞到 `gen_ai.client.time_to_first_token`
4. **Tool specs 落 span**：`StepMetric` 加 `tool_schemas` 字段；
   `record_llm_span` 时塞到 `gen_ai.request.tool_specs`
5. **Tool artifacts 落 span**：`record_tool_span` 加 `artifacts` 参数
6. **schema_version bump v1→v2**：旧 on-disk traces 启动时归档到
   `traces.v1.jsonl`，client 端不渲染
7. **Wire / HTTP 契约不变**：WS wire 仍只透传 `trace_id / turn_id`；
   全量 trace 仍走 HTTP `GET /debug/runs/<run_id>`

## 设计

### 1. Span 模型扩展

#### 1.1 `SpanKind` 新枚举（`core/observability/types.py`）

按「phase / logical / work」三类：

```python
class SpanKind(str, Enum):
    # ── Phase spans（每个 run 固定各 1 个）──
    BOOTSTRAP = "bootstrap"   # engine.run() 启动到第一个 user event
    LOOP = "loop"             # 包裹所有 _react 迭代
    FINALIZE = "finalize"     # end_run 起，output queue 排干

    # ── Logical container ──
    TURN = "turn"             # 一个 react step（每 _react iteration 一个）

    # ── Work ──
    REASONING = "reasoning"   # 单次 LLM 调用
    ACT = "act"               # 一个 turn 里所有 tool calls 的容器（仅 tool_calls > 0 时发）
    TOOL = "tool"             # 单次 tool 调用（取代旧的「一个 tool 一个 act」）
    COMPRESS = "compress"     # L1/L2 fold
```

`parent_id` 真层级（之前所有 span 都指向 turn_id，是假的层级）：

| Span | parent_id |
|---|---|
| bootstrap / loop / finalize | `run_id` |
| turn | `loop_span_id` |
| reasoning / act / compress | `turn_span_id` |
| tool | `act_span_id`（若 act 不存在则 `turn_span_id`） |

#### 1.2 `Run.schema_version`

`Run` 增加 `schema_version: int = 2`。`core/observability/jsonl_store.py` 启动时
检测 v1 行 → 跳过 + 写 warn log → 把整个 v1 file 重命名到 `<session>.traces.v1.jsonl`。
详见 §6。

### 2. OTel 属性命名（新增 `core/observability/otel_attrs.py` 集中常量）

> 这是**单一来源**——所有 record_* helper、engine 落点都从这里 import，
> 不允许散落字符串字面量。

#### 2.1 常量分类

```python
# 服务级（任何 span 都可带）
ATTR_SERVICE_NAME = "service.name"
ATTR_SESSION_ID   = "session.id"

# Agent / loop 自定义扩展（OTel 无对应；沿用 OTel 点分隔命名风格）
ATTR_LOOP_TURN_IDX          = "loop.turn.idx"
ATTR_LOOP_COMPRESS_LEVEL    = "loop.compress.level"
ATTR_LOOP_COMPRESS_SUMMARY  = "loop.compress.summary"
ATTR_LOOP_COMPRESS_FOLDED   = "loop.compress.folded_count"
ATTR_LOOP_COMPRESS_BUDGETS  = "loop.compress.budget_ids"
ATTR_LOOP_CANCELLED         = "loop.cancelled"        # bool，扩展 status 3 值用

# OTel GenAI 语义约定（gen_ai.* 命名空间）
ATTR_GENAI_REQUEST_MODEL             = "gen_ai.request.model"
ATTR_GENAI_REQUEST_MESSAGES          = "gen_ai.request.messages"          # list[dict]
ATTR_GENAI_REQUEST_TOOL_SPECS        = "gen_ai.request.tool_specs"       # list[dict]（扩展字段）
ATTR_GENAI_RESPONSE_MODEL            = "gen_ai.response.model"
ATTR_GENAI_RESPONSE_TEXT             = "gen_ai.response.text"            # str
ATTR_GENAI_RESPONSE_REASONING        = "gen_ai.response.reasoning"       # str | None
ATTR_GENAI_RESPONSE_FINISH_REASONS   = "gen_ai.response.finish_reasons"  # str（OTel 用复数但单值即可）
ATTR_GENAI_USAGE_INPUT_TOKENS        = "gen_ai.usage.input_tokens"       # int
ATTR_GENAI_USAGE_OUTPUT_TOKENS       = "gen_ai.usage.output_tokens"      # int
ATTR_GENAI_USAGE_CACHED_TOKENS       = "gen_ai.usage.cached_tokens"      # int
ATTR_GENAI_CLIENT_OPERATION_DURATION = "gen_ai.client.operation.duration"  # int (ms)
ATTR_GENAI_CLIENT_TIME_TO_FIRST_TOKEN = "gen_ai.client.time_to_first_token"  # int (ms)

# OTel Tool 语义约定（tool.* 命名空间）
ATTR_TOOL_NAME             = "tool.name"
ATTR_TOOL_CALL_ID          = "tool.call.id"
ATTR_TOOL_CALL_ARGUMENTS   = "tool.call.arguments"   # OTel 规定 string；MonoX 存 JSON string
ATTR_TOOL_RESULT           = "tool.result"           # OTel 规定 string；MonoX 存 JSON string
ATTR_TOOL_RESULT_STATUS    = "tool.result.status"    # 扩展（OTel 无）
ATTR_TOOL_RESULT_TRUNCATED = "tool.result.truncated" # 扩展（OTel 无）

# OTel Error 语义约定（error.* 命名空间）
ATTR_ERROR_TYPE    = "error.type"
ATTR_ERROR_MESSAGE = "error.message"
```

#### 2.2 旧 key → 新 key 映射表

| 旧 key | 新 key（OTel） |
|---|---|
| `model` | `gen_ai.request.model` + `gen_ai.response.model` |
| `messages` | `gen_ai.request.messages` |
| (新) | `gen_ai.request.tool_specs` |
| `response_text` | `gen_ai.response.text` |
| `reasoning_content` | `gen_ai.response.reasoning` |
| `usage` (dict) | 拆 3 个顶层 attr：`gen_ai.usage.input_tokens` / `output_tokens` / `cached_tokens` |
| `finish_reason` | `gen_ai.response.finish_reasons` |
| `latency_ms` | `gen_ai.client.operation.duration` |
| (新) | `gen_ai.client.time_to_first_token` |
| `tool_name` | `tool.name` |
| `args` (dict) | `tool.call.arguments`（**JSON string**，按 OTel 规定） |
| `result` (dict) | `tool.result`（**JSON string**，按 OTel 规定） + 提 `tool.result.status` / `truncated` 到顶层 attr |
| `level` (compress) | `loop.compress.level` |
| `summary` (compress) | `loop.compress.summary` |
| `folded_count` (compress) | `loop.compress.folded_count` |
| `budget_ids` (compress) | `loop.compress.budget_ids` |
| (新) | `error.type` / `error.message`（错误时填） |

**注意点：**

- `usage` 从嵌套 dict 改成顶层 3 个属性 —— OTel 约定
- `args` / `result` 从嵌套 dict 改成 JSON string —— OTel 规定 value 是 string
- `result.stdout/stderr/exit_code` 等内部细节仍存在 `tool.result` 这个 JSON string 里
  （保留完整 tool output），但 `status` / `truncated` 提到顶层 attr 方便快速查询

### 3. Engine 改造（`core/loop/engine.py`）

#### 3.1 三个 phase span

```python
async def run(self) -> None:
    # bootstrap：覆盖 checkpoint 恢复 + queue 初始化 + pumper 起 task
    self._run_id = await self._traces.begin_run(self._user_text_for_run())
    bs_id = await self._traces.begin_span(
        parent_id=self._run_id, kind=SpanKind.BOOTSTRAP, name="bootstrap",
        attributes={ATTR_SERVICE_NAME: "monox", ATTR_SESSION_ID: self._session_key},
    )
    try:
        # ... checkpoint restore + queue setup + create_task(pumper) ...
        await self._traces.end_span(bs_id, status="ok")
    except BaseException:
        await self._traces.end_span(bs_id, status="error")
        raise

    # loop
    loop_id = await self._traces.begin_span(
        parent_id=self._run_id, kind=SpanKind.LOOP, name="loop",
    )
    try:
        await self._react()   # 里头所有 begin_turn / record_* 都把 parent 串起来
        await self._traces.end_span(loop_id, status="ok")
    except BaseException as exc:
        await self._traces.end_span(loop_id, status="error", attributes={
            ATTR_ERROR_TYPE: type(exc).__name__, ATTR_ERROR_MESSAGE: str(exc),
        })
        raise

    # finalize
    fz_id = await self._traces.begin_span(
        parent_id=self._run_id, kind=SpanKind.FINALIZE, name="finalize",
    )
    try:
        await self._end_run_normal(final_text)  # 现有 end_run + 排干 output queue
        await self._traces.end_span(fz_id, status="ok")
    except BaseException:
        await self._traces.end_span(fz_id, status="error")
        raise
```

**注意**：`begin_run(user_text)` 从「lazy 第一次 user event 才触发」改成「`engine.run()`
入口同步建 run_id」。之前的 caller 不依赖 run_id 时机，这里改完不变行为。

#### 3.2 Turn span 显式化

当前 `begin_turn(turn_idx)` 返回 string、不发 span。改成发一个
`Span(kind=TURN, parent_id=loop_span_id)`，**新 turn_id == 新 span_id**
（保留旧接口的 string 类型返回值，零侵入）。

```python
async def _react(self):
    while ...:
        turn_id = await self._traces.begin_turn(self._step_idx)
        # turn_id 现在同时是 TURN span 的 span_id
        # 后续 record_llm_span / record_act_span / record_tool_span 都把 turn_id 当 parent_id
```

`record_llm_span` / `record_act_span` / `record_compress_span` 内部把 `turn_id`
当 parent_id，自动挂到正确的 TURN span 下。

#### 3.3 Act span 容器（仅 tool_calls > 0）

```python
async def _execute_tool_calls(self, tool_calls):
    if not tool_calls:
        return
    act_id = await self._traces.begin_span(
        parent_id=self._current_turn_id, kind=SpanKind.ACT,
        name=f"act:{len(tool_calls)}_calls",
    )
    try:
        for tc in tool_calls:
            tool_span_id = await self._traces.record_tool_span(
                parent_id=act_id,  # ← 关键：tool 挂 act 下
                tool_name=tc["function"]["name"],
                call_id=tc["id"],
                args=json.loads(tc["function"]["arguments"]),
                result=tool_result,
                artifacts=tool_result.artifacts,   # 新增
                latency_ms=...,
                status="ok" | "error" | "cancelled",
            )
        await self._traces.end_span(act_id, status="ok")
    except BaseException:
        await self._traces.end_span(act_id, status="error")
        raise
```

### 4. TTFT 采集（`core/llm_proxy/proxy.py` + `LlmChunk`）

#### 4.1 `LlmChunk` 加 `first_chunk_at_ms`

`core/protocol/__init__.py`（或 `core/protocol/events.py`，取决于 LlmChunk 的
实际定义位置）的 `LlmChunk` frozen dataclass 加字段：

```python
@dataclass(frozen=True)
class LlmChunk:
    delta_text: str | None = None
    delta_reasoning: str | None = None
    delta_tool_calls: tuple[dict, ...] = ()
    finish_reason: str | None = None
    usage: dict | None = None
    first_chunk_at_ms: int | None = None   # 新增；proxy 首个非空 delta 时填
```

默认值 `None` 保证向后兼容（旧 LlmChunk 构造不传这个字段也行）。

#### 4.2 proxy.py 采集点

```python
async def stream(self, messages, tools=None, options=None) -> AsyncIterator[LlmChunk]:
    request_t0 = time.monotonic()
    first_chunk_at: int | None = None
    async with client.stream("POST", "/chat/completions", json=payload) as resp:
        # ... existing resp.status_code check ...
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            chunk = self._parse_chunk(json.loads(data))
            # TTFT 采集点：首个有实际内容的 chunk（delta_text 或 delta_reasoning 非空）
            if first_chunk_at is None and (chunk.delta_text or chunk.delta_reasoning):
                first_chunk_at = int((time.monotonic() - request_t0) * 1000)
            yield chunk if first_chunk_at is None else replace(chunk, first_chunk_at_ms=first_chunk_at)
```

#### 4.3 engine 落 `StepMetric.ttft_ms` → span

```python
# core/loop/metric.py
@dataclass
class StepMetric:
    latency_ms: int = 0
    ttft_ms: int | None = None        # 新增
    tool_schemas: list[dict] | None = None  # 新增；record_llm_span 时塞到 gen_ai.request.tool_specs
```

```python
# core/loop/engine.py — 在 _react 消费 chunk 时记录
for chunk in stream_iter:
    if chunk.first_chunk_at_ms is not None and step_metric.ttft_ms is None:
        step_metric.ttft_ms = chunk.first_chunk_at_ms
    # ... existing delta accumulation ...
```

`record_llm_span` 时把 `step_metric.ttft_ms` 和 `step_metric.tool_schemas` 一起塞到
OTel attr。tool_schemas 来自 `self._tools.openai_schemas()`（已存在）。

### 5. Wire / HTTP 契约

**WS wire 不变**：

| Wire frame | 字段 |
|---|---|
| `StatusChange` | `trace_id`, `turn_id` |
| `MetricChunk` | `trace_id`, `turn_id`, `model` |
| `FinalMessage` | `trace_id` |

仍只透传 ID，不引入 trace data frame。

**HTTP 契约**：

| 路径 | 行为 |
|---|---|
| `GET /debug/runs/recent?session_key=X&limit=N` | `{"runs": [RunSummary, ...]}`，`RunSummary` 加 `schema_version` 字段；client 可据此过滤 v1 |
| `GET /debug/runs/<run_id>?session_key=X` | `Run.to_dict()` 全量；自动含 OTel-style attributes + `schema_version: 2` |

### 6. schema_version 策略

- 新写入的 Run 全部带 `schema_version: 2`
- 启动时 `JsonlTraceStore.__init__()` 检测 session 的 traces.jsonl：
  - 读到 v1 行（缺 `schema_version` 或 `schema_version < 2`）→ 跳过该行 + warn log
  - 整个文件都是 v1 → 把文件 rename 成 `<session>.traces.v1.jsonl`（归档，留给用户手动 grep）
  - 混合 v1 + v2 → 只跳过 v1 行，v2 继续读
- `core/observability/jsonl_store.py` 加 `_archive_v1(path)` helper
- client 端拿到 v1 run → 显示「该 trace 旧版不可读」（spec 见
  [trace-tree-redesign.md §6](../../../MonoDesk/spec/requirements/trace-tree-redesign.md)）

### 7. record_* helper 新签名（`core/observability/collector.py`）

```python
async def begin_span(self, parent_id, *, kind, name, attributes=None) -> str:
    """发一个 span（不结束）。返回 span_id。"""
    ...

async def end_span(self, span_id, *, status="ok", attributes=None) -> None:
    """结束 span，写 end_ts。可选补 attributes（error 时填 error.type/message）。"""
    ...

# 取代旧 record_act_span
async def record_tool_span(
    self, parent_id, *, tool_name, call_id, args, result, artifacts=None,
    latency_ms=0, status="ok",
) -> str:
    """发一个 Span(kind=TOOL) 并立即 end。返回 span_id。

    result: dict（ToolResult.to_dict 形式）
    artifacts: tuple[File, ...]（monoX 内部 File 对象；JSON-safe dict list 后塞到 tool.result 同一 JSON string）
    """
    ...
```

旧 `record_act_span` 改名 `record_tool_span`（语义更准）。`begin_turn` 内部
改成发 TURN span 而不是只生成 ID——保持 `begin_turn(idx) -> span_id` 旧接口
不变。

### 9. collector.py 内部 OTel 落点（伪代码）

```python
async def record_llm_span(self, turn_id, *, model, messages, response_text,
                           reasoning_content, usage, finish_reason,
                           ttft_ms, tool_schemas, latency_ms, status):
    attrs = {
        ATTR_GENAI_REQUEST_MODEL: model,
        ATTR_GENAI_REQUEST_MESSAGES: messages,
        ATTR_GENAI_REQUEST_TOOL_SPECS: tool_schemas or [],   # 新
        ATTR_GENAI_RESPONSE_MODEL: model,
        ATTR_GENAI_RESPONSE_TEXT: response_text or "",
        ATTR_GENAI_RESPONSE_REASONING: reasoning_content,
        ATTR_GENAI_RESPONSE_FINISH_REASONS: finish_reason or "unknown",
        ATTR_GENAI_USAGE_INPUT_TOKENS: (usage or {}).get("prompt_tokens", 0),
        ATTR_GENAI_USAGE_OUTPUT_TOKENS: (usage or {}).get("completion_tokens", 0),
        ATTR_GENAI_USAGE_CACHED_TOKENS: (usage or {}).get("cached_tokens", 0),
        ATTR_GENAI_CLIENT_OPERATION_DURATION: latency_ms,
        ATTR_GENAI_CLIENT_TIME_TO_FIRST_TOKEN: ttft_ms,       # 新
    }
    if status == "error":
        # 错误时 engine 传 exc_type / exc_msg 进来
        attrs[ATTR_ERROR_TYPE] = ...
        attrs[ATTR_ERROR_MESSAGE] = ...
    span_id = await self.begin_span(
        parent_id=turn_id, kind=SpanKind.REASONING,
        name=f"reasoning:{model}", attributes=attrs,
    )
    await self.end_span(span_id, status=status)


async def record_tool_span(self, parent_id, *, tool_name, call_id, args, result,
                            artifacts=None, latency_ms=0, status="ok"):
    # tool.result JSON string（OTel 规定）
    result_full = dict(result or {})
    if artifacts:
        result_full["artifacts"] = [a.to_dict() for a in artifacts]   # 旧丢的字段补回来
    result_json = json.dumps(result_full, ensure_ascii=False, default=str)
    attrs = {
        ATTR_TOOL_NAME: tool_name,
        ATTR_TOOL_CALL_ID: call_id,
        ATTR_TOOL_CALL_ARGUMENTS: json.dumps(args or {}, ensure_ascii=False, default=str),
        ATTR_TOOL_RESULT: result_json,
        ATTR_TOOL_RESULT_STATUS: result_full.get("status", "ok"),
        ATTR_TOOL_RESULT_TRUNCATED: bool(result_full.get("truncated", False)),
    }
    span_id = await self.begin_span(
        parent_id=parent_id, kind=SpanKind.TOOL,
        name=f"tool:{tool_name}", attributes=attrs,
    )
    await self.end_span(span_id, status=status)


async def record_compress_span(self, turn_id, *, level, summary, folded_count,
                                budget_ids, status="ok"):
    attrs = {
        ATTR_LOOP_COMPRESS_LEVEL: level,
        ATTR_LOOP_COMPRESS_SUMMARY: summary,
        ATTR_LOOP_COMPRESS_FOLDED: folded_count,
        ATTR_LOOP_COMPRESS_BUDGETS: list(budget_ids or []),
    }
    span_id = await self.begin_span(
        parent_id=turn_id, kind=SpanKind.COMPRESS,
        name=f"compress:{level}", attributes=attrs,
    )
    await self.end_span(span_id, status=status)
```

## 不变量

- **Wire frames 零变化** —— StatusChange / MetricChunk / FinalMessage schema
  不动；只是 collector 内部 + HTTP 响应内容升级
- **HTTP endpoint 路径不变** —— `/debug/runs/recent`、`/debug/runs/<id>`
- **`Run.status` 3 值保留** —— `running` / `ok` / `error` / `cancelled`
  （OTel 标准只 2 值，但 MonoX UI 需要 `cancelled` 状态展示，不动）
- **`Span.status` 3 值保留** —— 同上
- **`Turn` 数据类保留** —— 旧 `Turn(turn_id, turn_idx, spans)` 仍然在 Run 里
  用作 per-turn 分组；新模型下 turn_id 就是 TURN span 的 span_id，spans
  列表改为该 turn 下所有 spans（REASONING / ACT / TOOL / COMPRESS 等）
- **`core/observability/types.py` 是 frozen dataclass** —— 加字段时保持 frozen
- **TTFT 客户端估算保留** —— `engine.ts:493` 的 `firstTokenAt - userSendTime`
  是 client-side 估算（chat 顶栏用），server-side TTFT 是独立第二个来源
  （更准确，含 server-side processing time）
- **Backwards compat** —— pre-1.0 不保兼容；v1 traces 归档到
  `traces.v1.jsonl`，UI 不显示

## 不做的事

- **不引入 OTel SDK** —— 不引 `opentelemetry-api` / `opentelemetry-sdk`；
  纯字符串字面量 + dict 透传。理由：项目 runtime 端只产生 trace data 给 client
  渲染；不需要 export 到外部 collector。如果未来要接 Jaeger / Tempo，加 SDK 是
  单独的 v3 spec
- **不实现 Span batching / 异步上报** —— 现有 `JsonlTraceStore` per-session
  append-only + 50MB cap + 8MB tail-scan 已经够用
- **不做 "exponential backoff with jitter" 重试** —— 见 [llm-error-recovery.md](./llm-error-recovery.md)
- **不动 ErrorEvent code** —— `llm_error` / `internal_error` 协议层不变
- **不动 Wire Protocol** —— trace_id / turn_id 透传机制不变

## 验证

### 1. 单测

```bash
cd MonoX && uv run pytest tests/ -q
# 5 个 trace 相关测试文件全部更新 fixture + 新断言
```

**`tests/test_observability_types.py`**：
- Span / Turn / Run dataclass round-trip 加 `schema_version: 2`
- 新 SpanKind 5 个值 round-trip（bootstrap / loop / finalize / turn / tool）
- `parent_id` 真串起来：构造一个完整 6 层 span tree，验证每个 span 的
  parent_id 都指向正确的上层 span_id

**`tests/test_trace_collector.py`**：
- 旧 fixture `assert a["model"]` → `assert a[ATTR_GENAI_REQUEST_MODEL]`
- 新增 `record_tool_span` 测试：assert `a[ATTR_TOOL_CALL_ARGUMENTS]` 是
  JSON string（不是 dict），且能 `json.loads` 反解回原 dict
- 新增 `begin_span` / `end_span` 测试：error status 时可补 `error.type` /
  `error.message` 属性

**`tests/test_engine_trace_integration.py`**：
- 跑完整 engine → assert trace 里有 bootstrap / loop / finalize 3 个 span
- assert reasoning span 的 parent_id == turn_span_id
- assert tool span 的 parent_id == act_span_id（act 存在时）

**`tests/test_engine_act_compress_spans.py`**：
- assert `a[ATTR_TOOL_CALL_ARGUMENTS]` 是 JSON string
- assert `a[ATTR_TOOL_RESULT]` 是 JSON string 且 `json.loads` 后含
  `artifacts` 字段（之前的 bug 已修）
- mock LlmChunk 带 `first_chunk_at_ms`，assert reasoning span 有
  `ATTR_GENAI_CLIENT_TIME_TO_FIRST_TOKEN` 属性

**`tests/test_debug_server.py`**：
- assert `GET /debug/runs/<id>` 返回的 Run 含 `schema_version: 2`

### 2. 手工 e2e

```bash
cd MonoX && uv run python run.py
cd ../MonoDesk && npm run tauri dev
# 1) 发一条消息 → 等 final → 点 trace 按钮
#    预期：
#    - tree 节点：bootstrap / loop / turn #1 / reasoning / act / tool: bash / finalize
#    - 选 reasoning 节点 → 看到 INPUT: gen_ai.request.model / messages / tool_specs
#                        → 看到 OUTPUT: gen_ai.response.text / usage.* / time_to_first_token
# 2) 长会话（10+ turn）→ 看 trace tree 多层嵌套是否流畅
# 3) 故意配错 base_url → 看 trace 上 reasoning span 是否有 error.type / error.message
```

### 3. 回归

- `tests/test_interrupt.py` 不破（interrupt 跟本 spec 无关）
- `tests/test_e2e.py` 4/4 通过
- `tests/test_llm_error_recovery.py` 不破（LLM 错跟本 spec 无关）

### 4. v1 归档验证

```bash
# 模拟 v1 traces：在 ~/.monox/traces/<sk>/traces.jsonl 手动写一行没有 schema_version 的 JSON
# 重启 MonoX
# 预期：
# - 文件被 rename 成 <sk>.traces.v1.jsonl
# - 日志 warn: "archived v1 trace file: <sk>.traces.jsonl → <sk>.traces.v1.jsonl (N lines)"
# - /debug/runs/recent 返回空（v1 不展示）
```

## 进度

- [x] 设计：本文档
- [ ] 实现：未开始
- [ ] 单测：未开始
- [ ] 手工 e2e：未开始