# 2026-08-10 初始设计

## 需求

写一个自己的 agent runtime core：
- 通过飞书（未来 Slack / terminal）与 Agent 交互
- 运行在本地 docker，未来可上 cloud desktop
- 架构极致解耦合、精简
- Python 实现

## 关键设计决策

### 分层
Channel → Gateway → Loop → {Sandbox, LLMProxy}，单向依赖，不反向引用。

### 内置基础 tool（core）
- `bash` — 沙箱唯一执行入口
- `skill_load` — 按需加载 skill 详情
- `read_tool_result_budget` — 按需读取被 L1 压缩的工具结果

业务能力**不内置 tool**，全部走 skill + CLI。

### session_key
- 当前固定 `default`
- 是核心隔离维度（workspace / checkpoint / memory 都基于它）
- interface 预留多 session 扩展

### Skill 加载（混合策略）
- 启动：注入每个 skill 的 `name + 一行描述` 到 system prompt
- 按需：LLM 调 `skill_load(name)` → 返回完整 SKILL.md

### wait_io 替代 awaiting_human
Loop 等待外部输入（人类回复 / 定时器 / webhook）统一为 `wait_io` 状态，
任何 `InboundEvent` 唤醒。HITL 不单独设计，作为 wait_io 的特例。

### Memory
- 路径：`memory/<session_key>/`
- `Memory.md` 短小 + 索引区
- `notes/` 子目录存实际记忆
- LLM 通过 bash 写入
- system prompt 注入 Memory.md

### Checkpoint / Storage
- interface 先定，实现先用文件（jsonl / fs）
- 未来切 sqlite / 向量库只换 implementation

### LLMProxy
- interface: `stream(messages, tools) → AsyncIterator[LlmChunk]`
- v0 只调一个模型，对齐 OpenAI-compatible stream
- 内部 harness（retry/fallback/限流）后续加

### 可观测性
- `MetricChunk` 在 StreamEvent 里，metrics dict 通用
- Channel 决定如何展示

### core vs extensions 物理隔离
- core = 最小 runtime（Python）
- extensions = 业务能力（shell 脚本为主）
- skill 是 shell 脚本，不是 Python，进程边界天然隔离
- skill 不得 import core 的 Python 模块

### docker 化
- 单一 Dockerfile
- 单一 config.toml（channel 凭证 / llm endpoint / sandbox 路径）

## 状态机

```
idle → thinking → tooling → wait_io → thinking → ... → done
```

## 协议设计原则

- 单向数据流
- 不可变事件（`@dataclass(frozen=True)`）
- 控制信号分离（interrupt / wait_io 不混入数据流）

## 后续迭代点

- LLMProxy harness（retry / fallback / 限流）
- HITL 敏感操作识别规则
- Cloud desktop 切换（bash_runner 换 ssh / docker exec 实现）
- CheckpointStore / MemoryStore 切 sqlite / 向量库