# MonoX 架构设计

MonoX 是一个自托管的 agent runtime。本文档是架构的唯一权威说明。

---

## 1. 一句话架构

**系统 = 模块 + 协议。**

- **模块**：有边界、可替换的实现单元。
- **协议**：模块之间的稳定契约，用 `Protocol` + 不可变 `@dataclass(frozen=True)` 表达。
- 模块只通过协议交互，不直接依赖对方的具体实现。替换实现不改协议，就不动其他模块。

---

## 2. 架构总览

```
                  ┌──────────────────────────────┐
                  │           run.py             │  装配层
                  │  把 core + extensions 组装    │
                  └───────┬──────────────┬───────┘
                          │ 组装         │ 组装
               ┌──────────▼───┐      ┌───▼────────────┐
               │     core     │      │  extensions    │
               │  稳定内核     │      │  适配层         │
               └──────┬───────┘      └───┬────────────┘
                      │ 协议              │ 实现协议
                      └────────▲─────────┘
                               │
                    extensions 依赖 core 的协议
```

### 依赖方向（强制）

```
extensions ──► core（只依赖协议）
run.py     ──► core + extensions
core       ──► 只依赖 httpx / tomli，绝不 import extensions
```

### 三层的职责

| 层 | 职责 | 是否可换 |
|---|---|---|
| `core/` | 稳定内核：协议 + ReAct 引擎 + 存储/执行抽象。零 UI / 零 IM / 零 LLM SDK | 稳定，不轻易改 |
| `extensions/` | 适配层：channel adapter、skill | 可随意重写 |
| `run.py` | 装配层：实例化具体实现，注入 core | 每用户可改 |

---

## 3. 协议清单

这是 core 对外暴露的全部契约。所有跨模块通信都走它们。

| 协议 | 职责 | 当前实现 | 消费者 |
|---|---|---|---|
| `Channel` | 双向桥接外部信号与 core | `extensions/channels/*` | `gateway` |
| `LLMProxy` | 流式调用 LLM，产出 `LlmChunk` | `core/llm_proxy/OpenAIStreamProxy` | `loop/engine` |
| `Tool` | 可被 agent 调用的工具 | `core/loop/tools/*` | `loop/engine` |
| `CheckpointStore` | 对话历史持久化 + 恢复 | `core/loop/checkpoint.py` | `loop/engine` |
| `MemoryStore` | 长期记忆读写 | `core/memory/fs_store.py` | `loop/engine` |
| `SandboxRunner` | 执行 shell 命令 | `core/sandbox/bash_runner.py` | `loop/tools/bash.py` |

### 鸭子类型（为什么用 Protocol）

协议检查的是**结构**，不是继承关系：只要一个对象有 `run()` 且签名匹配，`isinstance(obj, SandboxRunner)` 就成立，不需要它继承任何基类。

这就是鸭子类型：不看它"是什么类"，只看它"能不能做这件事"。所以 adapter 接入不需要改 core，只需要满足方法签名。

---

## 4. 数据流

### 入站（外部 → agent）

```
外部信号
  → Channel.listen() 产出 InboundEvent
  → Gateway fan-in 进 input_queue
  → EventWrapper.wrap() 包装成 XML 文本
  → 追加进 messages[]
  → LoopEngine 推理
```

### 出站（agent → 外部）

```
LoopEngine 产出 StreamEvent（TokenChunk / ToolEnd / FinalMessage / ...）
  → Gateway fan-out
  → EventWrapper.parse_output() 解析 <send channel="xxx">
  → 路由到对应 Channel.send()
```

入站统一包装，出站显式路由。无 `<send>` 标签时回 `pending_channel`。

---

## 5. 数据结构契约

当前集中定义在 `core/protocol/events.py`，按域分组如下。量还小，单文件单一 import 源；等协议域变多再拆。

| 域 | 数据结构 | 方向 |
|---|---|---|
| 入站 | `InboundEvent` | Gateway → Loop |
| 出站流 | `TokenChunk` `ReasoningChunk` `ToolStart` `ToolEnd` `StatusChange` `MetricChunk` `FinalMessage` `Card` `ErrorEvent` | Loop → Gateway |
| 工具 | `ToolCall` `ToolResult` | Loop ↔ Sandbox |
| LLM | `LlmChunk` | LLMProxy → Loop |
| Checkpoint | `CheckpointRecord` | Loop → CheckpointStore |
| Sandbox | `SandboxResult` | SandboxRunner → BashTool |

全部 `@dataclass(frozen=True)`，单向数据流，错误用 `status` 字段表达。

---

## 6. core 子模块

core 内部同样按「子模块 + 协议」组织。

| 子模块 | 职责 | 协议角色 |
|---|---|---|
| `protocol/` | 定义全部协议 + 数据结构 | 协议持有者 |
| `channel/base.py` | 定义 `Channel` 协议 | 协议定义 |
| `event_wrapper.py` | 入站 XML 包装 / 出站 `<send>` 路由 | 纯函数，无协议 |
| `gateway/` | 单/多 channel fan-in / fan-out | 消费 `Channel` |
| `loop/` | ReAct 主循环 + 工具注册 | 消费 `LLMProxy`/`Tool`/`CheckpointStore`/`MemoryStore` |
| `llm_proxy/` | OpenAI-compatible 流式调用 | 实现 `LLMProxy` |
| `sandbox/` | bash 执行后端 | 实现 `SandboxRunner` |
| `memory/` | 记忆存储 | 实现 `MemoryStore` |
| `config.py` | 统一 `config.toml` 加载 | 无协议 |

---

## 7. LoopEngine 细化

`LoopEngine` 是 core 的调度核心，不依赖任何具体实现，只依赖协议。

```
InboundEvent
     │
     ▼
┌──────────────────────────────────────┐
│            LoopEngine.run            │
│  while True:                         │
│    ev = await input_queue.get()      │
│    final = await _react(...)         │
│    output_queue.put(FinalMessage)    │
└──────────────┬───────────────────────┘
               ▼
        _react（ReAct 循环）
```

### ReAct 状态机

```
              ┌────────────► thinking ─────────────┐
              │                                    │
       有 tool_calls                        无 tool_calls
              │                             且无新事件
              ▼                                    │
          tooling ──工具执行完──► thinking          ▼
              │                                wait_io
              │ wait_io 工具                      │
              └────────► wait_io ◄──新 InboundEvent─┘
```

每一步：

1. drain `input_queue`，聚合新用户消息
2. `assemble_messages`（system + memory + skill summary + messages）
3. `LLMProxy.stream()` 流式产出文本 / tool_calls / reasoning
4. 无 tool_calls 且无新事件 → 写 checkpoint，进 `wait_io`
5. 有 tool_calls → 逐个 `Tool.execute()` → 写 checkpoint → 回到 thinking

### wait_io 设计

`wait_io` 是一个内置 tool，含义是「agent 主动结束当前 turn，等待外部输入」。

- agent 调用 `wait_io` 时，engine **不真正执行**它，直接标记 `has_wait_io`
- 当前 step 结束后进入 `wait_io` 状态，`_react` 返回
- 新 `InboundEvent` 到达时，`LoopEngine.run` 再次进入 `_react` 继续

这样 agent 可以自己控制「一个 turn 该停在哪」，而不是被 max_steps 或空输入推着走。

---

## 8. 关键存储设计

### CheckpointStore：对话历史持久化

- 职责：把 `messages` + `step_idx` 存下来，重启后恢复，让对话不丢。
- 当前实现 `JsonlCheckpointStore`：每步追加一条 JSONL，`load_latest` 读最后一条。
- 未来可换 sqlite，协议不变。

### MemoryStore：长期记忆

- 职责：agent 的长期记忆，跨 session 沉淀。
- 协议只定义 `read_index / write_note / update_index`。
- 当前实现 `FsMemoryStore` 用文件系统（`Memory.md` 索引 + `notes/` 目录）。
- **文件只是实现之一**，未来可换向量库等后端。

### SandboxRunner：命令执行

- 职责：执行 shell 命令，返回 `SandboxResult(stdout, stderr, exit_code)`。
- 当前实现 `BashRunner` 用 `asyncio.create_subprocess_shell`。
- 未来可换 docker exec / ssh，协议不变。

---

## 9. extensions 细化

| 子模块 | 职责 | 协议角色 |
|---|---|---|
| `channels/` | `terminal.py` `textual_chat.py` `feishu.py` | 实现 `Channel` |
| `skills/` | `SKILL.md` + shell 脚本 | 不实现协议，经 `bash`/`skill_load` 被调用 |

新增 channel：实现 `Channel`，`run.py` 加 `kind` 分支。
新增 skill：放 `extensions/skills/<name>/SKILL.md`。

---

## 10. 边界规则

1. core 不 import extensions。
2. core 只依赖 `httpx` + `tomli`。
3. 跨模块事件 `@dataclass(frozen=True)`，单向数据流。
4. 接口用 `Protocol` + `runtime_checkable`，不强制继承。
5. 新增协议属于 core 稳定 API 变更，先确认不是 adapter 该做的事。
6. 错误用返回 `status` 字段，不用异常控制流。

---

## 11. 未实现（按依赖顺序）

| 项 | 状态 | 文档 |
|---|---|---|
| L1/L2/L3 上下文压缩 | L1 函数已就位，engine 未调用 | `requirements/context-compression.md` |
| 干净退出 shutdown_event | 未实现 | `requirements/shutdown.md` |
| LLM harness（retry/fallback/rate-limit） | 未实现 | `requirements/llm-harness.md` |
| Memory 自动摘要/维护 | 未实现 | `requirements/context-compression.md` |
| bash 危险命令 HITL hook | 未实现 | `goal.md` 中期目标 |
