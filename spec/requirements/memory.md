# memory: 长期记忆 / Memory.md 维护策略

## 问题

跨 session 的事实性信息（「这个 repo 用 uv」「用户偏好英文回复」「项目里有个隐藏的 .env」）目前没法沉淀：

- 单 session 内对话历史靠 checkpoint 持久化（jsonl append，详见 `core/loop/checkpoint.py`）
- 跨 session 没有任何 recall
- `extensions/skills/memory-write` 提供了手写接口，但「什么时候写 / 写什么 / 怎么 recall」没定义

## 现状（v0.9）

- `core/memory/fs_store.py` 已实现 `FsMemoryStore`（Protocol: `read_index` / `write_note` / `update_index`）
- 目录布局：`<root>/<session_key>/Memory.md`（索引） + `<root>/<session_key>/notes/<name>.md`（详细）
- engine 启动时把 `Memory.md` 内容塞进 system message
- `memory-write` skill 让 LLM 主动调用 `write_note` / `update_index`

## 问题细化

1. **自动维护缺失**：LLM 不会自动写 memory（除非 prompt 里强制要求；且 prompt 一次只能塞少量，跨 session 难）
2. **recall 粒度**：`Memory.md` 是单文件全量读，无分层（如同 session 内的 fact 和跨 session 的 fact 混在一起）
3. **冲突解决**：LLM 写新 note 时可能与旧 note 矛盾（项目改名、用户换偏好），无 review
4. **search 缺失**：长 session 后 notes 几十个，索引只放标题没用，得有 keyword recall

## 设计（分层 + 半自动）

### 三层结构

```
.monox/memory/<session_key>/
├── Memory.md           # L0：每次启动都注入 system（≤ 1KB）
├── facts/              # L1：单条事实（≤ 200 字），有 ts 和 source
│   ├── 2026-08-10-use-uv.md
│   ├── 2026-08-10-prefer-english.md
│   └── ...
└── notes/              # L2：长文 / 主题总结（按需 recall）
    ├── project-structure.md
    ├── bash-gotchas.md
    └── ...
```

- **L0**（index）：engine 每次启动塞 system，单 session 的关键事实 ≤ 1KB
- **L1**（facts）：单条事实，写时标 `ts` + `source_turn`（哪 turn 由谁写入）
- **L2**（notes）：长文 / 主题，由 L1 聚合，或 LLM 主动写

### 写入路径

| 触发 | 写入 | 谁负责 |
|---|---|---|
| LLM 调 `write_note` / `update_index` | 立即落 L0/L1/L2 | memory-write skill |
| L2/L3 压缩触发（参见 context-compression.md） | 让 LLM 同时维护 Memory.md（L3） | engine |
| 用户显式 `remember <fact>` | 立即 L1 | 新 tool：`remember_tool` |
| 周期（每 10 turn） | 「review 一下最近 10 turn，有要写 memory 的吗？」 | engine |

### Recall 路径

- 启动：L0 全量注入
- 每 turn：L1 用 BM25 / 简单 keyword match，命中 top-3 注到 system 末尾（**不**全量，避免污染）
- L2：LLM 调 `recall_note(name)` 显式拿（避免噪音）

### LLM 维护接口

`memory-write` skill 增加：
- `remember_fact(text, source_turn=None)` → 写 L1（带 ts）
- `forget_fact(keyword)` → 删 L1
- `update_index(content)` → 改 L0
- `write_note(name, content)` → 已有，写 L2
- `recall_note(name)` → 读 L2

### FsMemoryStore 扩展

```python
class FsMemoryStore(MemoryStore):
    # 已有：read_index / write_note / update_index
    async def list_facts(self, session_key) -> list[FactMeta]: ...
    async def search_facts(self, session_key, query) -> list[FactMeta]: ...
    async def write_fact(self, session_key, text, source_turn=None) -> None: ...
    async def delete_fact(self, session_key, name) -> None: ...
```

### 与 L3 压缩协同

context-compression.md 的 L3 让 LLM 在压缩老 turn 时同时维护 Memory.md（新增 fact / 删除过期）。memory.md 的写入触发条件之一就是 L3 触发。

## 验证

- 单测：`FsMemoryStore.write_fact` 后 `list_facts` 正确
- 单测：BM25 keyword match 命中
- e2e：mock channel → 输入 `remember: 用户偏好英文` → 退出 → 再启 → system message 含此 fact
- 回归：原有 e2e 4/4 不破

## 风险

- LLM 写 memory 质量不稳（写废话 / 写过时 / 重复）→ ts + source_turn 可定位；review 机制兜底
- recall 注到 system 增加 token → top-3 限制 + 命中阈值
- L0 膨胀 → 1KB 硬限；超出时强制让 LLM 拆 note + 摘要 L0

## 进度

- 设计：本文档
- 实现：未开始（中期目标）