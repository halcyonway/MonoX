# memory: 跨会话长期记忆（system prompt 注入 + bash 写）

## 目标

让 agent 拥有**跨会话**的长期记忆，且记忆的写入是**低频、显式**行为。

- **跨会话**：Memory.md 是**全局单例**，不按 `session_key` 分目录。无论你在 terminal、
  monodesk、feishu 哪个 channel 里，所有 session 共享同一份长期记忆。
- **低频、显式**：memory 写入不是自动行为。压缩摘要**不**落 Memory.md。Memory.md 的内容增长
  只来自两个来源：
  1. 用户**显式**说「记住 / remember / save this / 别忘了」等。
  2. agent 自己在对话中识别出一个**持久**事实（用户偏好 / 项目约定 / 踩过的坑），且这个事实
     在跨会话层面有价值——agent 主动用 Bash 写入。

## 设计

### 物理路径

四个独立的 root，**完全互不嵌套**：

```
.monox/
├── memory/                       ← 用户长期记忆（跨会话）
│   ├── Memory.md                 # 全局索引（短，注入 system prompt）
│   └── notes/                    # 全局细节文件
│       └── <topic>.md
├── workspace/<session_key>/      ← LLM 视角的 shell cwd（per-session）
├── state/<session_key>/          ← Runtime 视角的 internal state（per-session，LLM 不可见）
│   └── checkpoint.jsonl          # 会话 messages 持久化（idle 后恢复用）
├── traces/<session_key>/         ← 开发者视角的可观测 trace（per-session）
│   └── traces.jsonl
├── skills/                       ← skill 目录
└── tmp/                          ← scratch
```

- **memory/**：跨会话全局。`Memory.md` 注入 system prompt；`notes/<topic>.md` 存放详情。
- **workspace/<sk>/**：per-session。LLM 调 `BashTool` 时的 cwd；LLM 可写、可删。
- **state/<sk>/**：per-session。`checkpoint.jsonl` 落这里——Runtime 内部 state，**跟 shell cwd 完全隔离**，
  LLM 任何 cwd 都见不到这文件，不会因为 `rm -rf .` 误删。
- **traces/<sk>/**：per-session，独立 root。traces 是开发者观测视角，**跟用户记忆没关系**，不该混。

四个 root 在 `config.toml`：

```toml
[sandbox]
workspace_root = "./.monox/workspace"
memory_root    = "./.monox/memory"
state_root     = "./.monox/state"
traces_root    = "./.monox/traces"
```

### 注入：system prompt 的 `## Memory` section

每次 LLM step 开始前：

```python
memory_index = await memory.read_index(session_key)   # 读 .monox/memory/Memory.md
messages = assemble_messages(system, memory_index, skill_summary, messages)
# inject "## Memory" section + memory_index 内容
```

`assemble_messages` (`core/loop/context.py`) 现在生成这样一个 section：

```
## Memory

`Memory.md` (injected above, under this section) is your long-term **cross-session** memory.
Its body is a sparse index: each line `- topic: notes/x.md` points to a detail file in `notes/`.

Read: `Memory.md` is already injected. To fetch a specific topic's detail file, use Bash
(`cat .monox/memory/notes/<topic>.md`).

Write — low-frequency, explicit only:
- The user says 记住 / remember / save this / 别忘了, or
- You learn a durable preference, project convention, or gotcha worth keeping across sessions.

To remember: write the detail file under `notes/`, then append one line to the `Memory.md` index.
Do not auto-summarize the conversation, do not write ephemeral task state, do not write raw data.
```

system prompt 层面**只放这个用法说明**；真正的索引内容（`Memory.md` 全文）紧跟其后被 read_index 拼上。

### 写入：通过 Bash（不暴露 tool）

写入是 LLM 通过 Bash 直接操作 `.monox/memory/` 下的文件，**不**通过 `MemoryStore` Protocol，
也不通过任何 tool / skill。理由：

- 写入路径单一固定，没有值得抽象成 tool 的逻辑。
- 用 Bash 直接 `cat >> notes/x.md << EOF ... EOF` 跟普通文件编辑无差，agent 早就会。
- 把它做成 skill 是机制膨胀——system prompt 一段话讲清楚就够。

典型用法：

```bash
# 写详情
cat > .monox/memory/notes/project-conventions.md << 'EOF'
# Project Conventions

- Python 3.11 + uv for deps
- TUI: prompt_toolkit on stdio
EOF

# 加索引行
printf -- '- Project conventions: see notes/project-conventions.md\n' \
  >> .monox/memory/Memory.md
```

### 首次运行 / 初始化

Runtime 启动时 `_ensure_dirs` 会：

1. 一次性建好四个 root（`workspace_root` / `memory_root` / `state_root` / `traces_root`）
   及各 per-session 子目录（`workspace/<sk>/`、`state/<sk>/`、`traces/<sk>/`、
   `memory/notes/`）。
2. 如果 `.monox/memory/Memory.md` 不存在，写入一行 starter：
   ```
   # Memory index
   (empty — write your first topic when ready)
   ```

效果：LLM `cat .monox/memory/Memory.md` 总能读到当前状态（空 vs 有内容），不会因为
`cat: ... No such file` 误以为「memory 坏了」。`scripts/install.sh` 也同步：
建四个 root 目录，但**不**预填 Memory.md——确保 `run.py` 是 Memory.md 内容的唯一写入方，
避免 install 时和运行时分叉。

### 格式约定

- `Memory.md`：**稀疏索引**。每行一条，格式 `- <主题>: notes/<name>.md`。
  整个文件 < 50 行（或 < 1 屏）为宜。
- `notes/<name>.md`：完整描述。可以有 markdown 标题、列表、代码块。
- 不使用 `## Conversation Summaries` 这类自动堆积区。摘要信息属于压缩层，
  落 `.jsonl` trace，不属于 Memory.md。

### 旧 L3 已删除

之前 `CompressionService.maintain_memory` 在 L2 压缩时**自动**把 summary `append_fact`
写进 Memory.md（产生 `## Conversation Summaries` 区）。这次决定**完全删除**该行为：

- L2 仍然做（折叠最早 N 轮 + 拿 summary 文本给可观测性 span 用）。
- L2 不再落 Memory.md。
- 原因：自动摘要把 Memory.md 灌成对话流水，违背「Memory.md 是用户驱动稀疏索引」的语义；
  且自动行为与「写是低频、显式」的设计冲突。

## 接口

### `MemoryStore` Protocol（`core/protocol/storage.py`）

```python
class MemoryStore(Protocol):
    async def read_index(self, session_key: str) -> str:
        """读 .monox/memory/Memory.md，文件不存在返回空串。"""

    async def write_note(self, session_key: str, name: str, content: str) -> None:
        """写 .monox/memory/notes/<name>。"""

    async def update_index(self, session_key: str, content: str) -> None:
        """整体替换 .monox/memory/Memory.md。"""
```

> `session_key` 参数保留作兼容壳，**不再用于路径分段**——所有方法都走全局路径。
> 调用方传 `"default"` 即可。未来若要做多 workspace / 多 memory root，可在该参数上扩展。

### 删除的能力

- `MemoryStore.append_fact`：删除（无调用方）。
- `CompressionService.maintain_memory`：删除（被 L3 删掉，仅留 L1/L2）。
- `extensions/skills/memory-write/` 与 `.monox/skills/memory-write/`：删除（机制已在 system prompt 自带，不应再做 skill）。

## 边界

- **不改**：`core/protocol/events.py` / `core/llm_proxy/` / `core/sandbox/` / `core/runtime_*.py`。
- **保留 per-session**：checkpoint.jsonl / traces.jsonl 仍然按 session_key 分目录。
- **不引入**：写 memory 的 tool（不要 tool）、向量检索 / 语义检索（v1 不做）。
- **不持久**：跨进程状态——Memory.md 是纯文本文件，运行时不需要额外缓存。

## 与现有文档的关系

- `core/protocol/storage.py` 的 docstring：已是新设计的 canonical 描述（替换前的版本仅一句话提到原则）。
- `spec/requirements/context-compression.md`：更新过——L3 删除的结论在这里说明；本文档给出 Memory.md 视角。
- `spec/ARCHITECTURE.md` §8：未来可同步加一句「Memory.md 是跨会话、不参与压缩」。
- `README.md`：把「记忆」feature 行指向 `spec/requirements/memory.md`。

## 验证

```bash
# 1. 跑测试，确保 L1/L2 不受影响、L3 调用方全去掉
uv run pytest -q

# 2. 灌一段 minimal smoke（不需要真跑 run.py）
python - <<'PY'
import asyncio
from pathlib import Path
from core.memory import FsMemoryStore

async def main():
    root = Path("/tmp/mem_smoke")
    import shutil; shutil.rmtree(root, ignore_errors=True)
    s = FsMemoryStore(root)
    # 写笔记
    await s.write_note("default", "python-version.md", "# Python\n项目用 3.11 + uv\n")
    # 更新索引
    await s.update_index("default", "# Memory index\n- python version: notes/python-version.md\n")
    # 读
    print(await s.read_index("default"))
    print((root / "notes" / "python-version.md").read_text())

asyncio.run(main())
# 输出：
# # Memory index
# - python version: notes/python-version.md
# # Python
# 项目用 3.11 + uv
PY
```

## 风险

- **写并发**：如果 agent 同时有多个 turn 在跑，可能同时改 Memory.md。File-level 上 write 不是 atomic，
  可能出现半截行。v1 不做文件锁；接受小概率读写竞态（可观测问题后再加锁）。要降低风险：turn 处理是单协程的。
  通常不会出现并发写。
- **Memory.md 无界增长**：索引本身保持稀疏是约定，agent 不严格遵守就会膨胀。v1 靠 usage section 提示。
- **注入 size 膨胀**：Memory.md 越长，每次 LLM call 注入越多 token。约定 < 1 屏的索引规模
  限制 ~50 个主题；超过就让 LLM 自己裁剪（向前端「把过期主题移到 `notes/archive/`」）。
- **session 维度反转**：之前的 `FsMemoryStore` 是 per-session 的，这次反转成全局。如果有人写脚本
  直接拼 `<root>/<session_key>/Memory.md` 路径，会失效——但因为代码路径只剩 `FsMemoryStore.read_index`
  一个入口，影响可控。
