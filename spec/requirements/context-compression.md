# context-compression: 会话内上下文压缩（L1/L2）

## 目标与边界

- 压缩只作用于**当前 session** 的 `messages`，不写跨 session Memory。
- 跨会话记忆由 `MemoryStore.read_index` 注入到 system prompt（见 `memory.md`）；压缩不碰。
- 因此**不再有 L3**。`CompressionService` 不依赖 `MemoryStore`。

## 协议

新增 `core/protocol/compression.py`，定义压缩域的数据结构与接口，`LoopEngine` 只依赖协议，不依赖具体 `CompressionService`。

```python
@dataclass(frozen=True)
class SessionSummary:
    entries: tuple[str, ...] = ()

    def append(self, summary: str) -> "SessionSummary": ...
    def as_prompt(self) -> str: ...
    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SessionSummary": ...


@dataclass(frozen=True)
class CompressionResult:
    messages: tuple[dict[str, Any], ...]  # 折叠后的当前会话消息
    session_summary: SessionSummary       # 新的会话摘要（含本次）


@runtime_checkable
class Compressor(Protocol):
    # L1
    def compress_tool_result(self, result: ToolResult) -> ToolResult: ...

    # L2 判断
    def should_compress(self, messages: list[dict[str, Any]]) -> bool: ...
    def should_precompute(self, messages: list[dict[str, Any]]) -> bool: ...

    # L2 执行
    async def compress(
        self,
        messages: list[dict[str, Any]],
        session_summary: SessionSummary,
    ) -> CompressionResult: ...

    # 异步预压缩
    async def start_precompute(
        self,
        messages: list[dict[str, Any]],
        session_summary: SessionSummary,
    ) -> None: ...
    def take_precomputed(
        self,
        messages: list[dict[str, Any]],
        session_summary: SessionSummary,
    ) -> CompressionResult | None: ...
    def cancel_precompute(self) -> None: ...
```

`SessionSummary` 序列化（用于 checkpoint）：

```json
{"version": 1, "entries": ["summary1", "summary2"]}
```

## Service

`core/loop/compression.py` 实现 `Compressor`。依赖 `LLMProxy` + `ReadToolResultBudgetTool`，**不再依赖 `MemoryStore`**。

```python
class CompressionService:
    def __init__(
        self,
        *,
        budget_tool: ReadToolResultBudgetTool,
        llm: LLMProxy,
        l1_truncate_len: int = 4000,
        l2_soft_chars: int = 16_000,   # 软阈值：提前后台压
        l2_hard_chars: int = 24_000,   # 硬阈值：必须压
        l2_keep_turns: int = 2,
        summary_options: dict[str, Any] | None = None,
    ): ...
```

### 方法语义

| 方法 | 语义 |
|---|---|
| `compress_tool_result` | L1：超阈值截断 + `budget_id` |
| `should_compress` | 是否达到硬阈值（必须压缩） |
| `should_precompute` | 是否达到软阈值（可启动后台预压缩） |
| `compress` | 同步压缩；先取消在途 precompute，再执行 |
| `start_precompute` | 启动后台预压缩任务；已在跑则 no-op |
| `take_precomputed` | 非阻塞取已完成且仍匹配当前消息的预压缩结果；不匹配返回 `None` |
| `cancel_precompute` | 取消后台任务，清空缓存 |

### 异步预压缩 + 同步兜底

内部维护 `_precompute_task / _precomputed / _precomputed_key`。

- `start_precompute`：把当前 `messages` 快照 + `session_summary` 打包，创建后台 task；key = `json.dumps((session_summary.entries, fold_block), sort_keys=True)`。
- `take_precomputed`：只有当后台任务完成、且当前 `messages` 算出的 key 与预压 key 一致，才复用；否则返回 `None`。
- `compress`：`cancel_precompute()` 后同步执行。

`_compute(messages, session_summary)`：

1. `_fold_earliest_turns(messages)` 得到 `(fold_end, block)`
2. `fold_end <= 0` → 原样返回
3. `summary = await _summarize(block)`
4. `new_summary = session_summary.append(summary)`
5. 返回 `CompressionResult(messages=tuple(messages[fold_end:]), session_summary=new_summary)`

## Loop 接入

`LoopEngine` 持有 `self._session_summary: SessionSummary` 与 `self._compression: Compressor`。

每个 react step，`assemble_messages` 之前：

```python
if self._compression.should_compress(self._messages):
    await output_queue.put(StatusChange(state="compressing"))

    result = self._compression.take_precomputed(
        self._messages, self._session_summary
    )
    if result is None:
        result = await self._compression.compress(
            self._messages, self._session_summary
        )

    self._messages = list(result.messages)
    self._session_summary = result.session_summary

elif self._compression.should_precompute(self._messages):
    await self._compression.start_precompute(
        self._messages, self._session_summary
    )
```

然后组装：

```python
memory_index = await self._memory.read_index(self._session_key)
messages = assemble_messages(
    self._system,
    memory_index,
    self._skill_summary,
    self._session_summary.as_prompt(),   # 会话摘要，非 Memory
    self._messages,
)
```

L1 接入点不变：`tool.execute` 之后、append tool 消息之前调用 `compress_tool_result`。

## Checkpoint 兼容

`compressed_snapshot` 存会话摘要，schema：

```json
{"version": 1, "entries": ["summary1", "summary2"]}
```

**保存**：两处 `checkpoint.save`（final 分支 + tool dispatch 分支）都传：

```python
compressed_snapshot=self._session_summary.to_dict() or None
```

**恢复**：

```python
ck = await self._checkpoint.load_latest(self._session_key)
if ck is None:
    return
self._messages = list(ck.messages)
self._step_idx = ck.step_idx + 1
self._session_summary = SessionSummary.from_dict(ck.compressed_snapshot)
```

关键点：checkpoint 里存的是**压缩后**的 `messages` + 对应的 `session_summary`。重启后不重复压缩，也不丢已折叠内容。

## Memory 解耦

- 删除 `CompressionService.maintain_memory` 与 `memory` 依赖。
- 删除 `MemoryStore.append_fact` 及 `FsMemoryStore.append_fact`。
- `assemble_messages` 新增 `session_summary_text: str` 参数，注入 system 的会话摘要段，替代之前「写 Memory.md 再 read_index」的 L3 路径。

## 验证

- L1：长 stdout/stderr 截断 + `budget_id` 可读回完整结果。
- L2：软/硬阈值判断；异步 precompute 命中复用、不命中走同步兜底。
- `SessionSummary`：append / to_dict / from_dict roundtrip。
- checkpoint：压缩后保存 → 重启恢复 `messages` + `session_summary`。
- e2e：多 turn 触发 L2，重启后上下文不丢；压缩不写 Memory.md。

## 风险

- 异步 precompute 必须用 `block_key` 校验，避免用旧摘要折叠新消息。
- `compress` 与后台 precompute 竞态：`compress` 先 cancel，保证只有一条摘要路径。
- checkpoint JSON 需兼容旧数据（`compressed_snapshot` 可能为 `None`）。
