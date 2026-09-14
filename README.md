# MonoX

A minimal, self-use Agent Runtime. Code it, scratch it, keep it lean.

**系统 = 模块 + 协议。** Runtime 进程跑核心（协议 + ReAct 引擎 + 存储/执行抽象），channel 作为 feature 通过协议接入。

[MonoDesk](https://github.com/halcyonway/MonoDesk) 是桌面 UI channel（独立仓库，直接 ws 连 RuntimeServer，不走 in-process adapter）。

## Design philosophy

- **Stable core, swappable adapters** — `core/` is the zero-UI / zero-IM / zero-LLM-SDK kernel. `extensions/` holds adapters that can be rewritten freely. `run.py` is the assembly layer.
- **Protocol-first, duck-typed** — Cross-module boundaries use `Protocol` + frozen dataclasses. If it quacks like the protocol, it IS the protocol. Swapping an implementation touches nothing else.
- **Runtime is the WS hub** — `RuntimeServer` is a plain ws server on :8765. Channels connect as clients; the runtime knows nothing about specific channel protocols.
- **Multi-session in one process** — Multiple channels share the main session via `last_active_source` fan-out, or use independent `session_key`s.
- **Config-driven** — `config.toml` owns all connection params. `run.py` reads only LLM / server / sandbox sections.

Full design: `spec/ARCHITECTURE.md` (§2 overview / §6 core modules / §9 extensions / §12 multi-session).

## 架构

```mermaid
graph TB
    subgraph Core["core/ (stable kernel)"]
        Protocol["protocol/<br/>events + dataclasses"]
        Loop["loop/<br/>ReAct engine + tools"]
        LLMP["llm_proxy/<br/>OpenAI stream"]
        Sandbox["sandbox/<br/>bash exec"]
        Memory["memory/<br/>long-term memory"]
        Server["runtime_server/<br/>ws server :8765"]
        Session["session_manager/<br/>multi-session + idle"]
        Health["health_server/<br/>:8767"]
    end

    subgraph Extensions["extensions/ (swappable adapters)"]
        Channels["channels/<br/>in-process:<br/>terminal / feishu / textual_chat"]
        Skills["skills/<br/>(no built-in skills)"]
    end

    Run["run.py<br/>assembly"]

    Loop --> Protocol
    LLMP --> Protocol
    Sandbox --> Protocol
    Memory --> Protocol
    Server --> Protocol
    Session --> Server
    Health --> Session

    Channels -.RuntimeWSClient.-> Server
    Skills -.-> Loop

    Run --> Core
    Run --> Channels

    classDef core fill:#e8f4f8,stroke:#333,stroke-width:2px
    classDef ext fill:#fdf3e7,stroke:#333,stroke-width:1px
    classDef run fill:#f0f0f0,stroke:#555
    class Protocol,Loop,LLMP,Sandbox,Memory,Server,Session,Health core
    class Channels,Skills ext
    class Run run
```

> 桌面 UI（MonoDesk）不通过 in-process channel 接入 —— 它走外部 ws client 直接连 :8765，
  因为桌面 UI 跟 Runtime 不在同一进程（用户在 Mac/Win/Linux 桌面端跑 UI，本地跑 Runtime）。
 协议一样，进程模型不同。

## Quick start

```bash
# 1. Prepare
./scripts/install.sh

# 2. Configure
export MINIMAX_API_KEY=eyJ...
# Edit config.toml: api_base / api_key / model

# 3. Start Runtime（启 ws server :8765）
uv run python run.py

# 4. Connect a channel
# 4a. terminal TUI（in-process channel，独立进程）
uv run python -m extensions.channels.terminal

# 4b. MonoDesk 桌面 UI（外部 ws client，独立仓库）
# 见 https://github.com/halcyonway/MonoDesk

# 5. Check Runtime status
curl http://127.0.0.1:8767/health
```

## Channels

| Channel | Description | 进程模型 |
|---|---|---|
| terminal | stdio TUI | in-process（独立进程跑 RuntimeWSClient 连 :8765） |
| feishu | lark-oapi | in-process（独立进程跑 RuntimeWSClient 连 :8765） |
| textual | textual full-screen TUI | in-process（独立进程跑 RuntimeWSClient 连 :8765） |
| **monodesk** | 桌面 UI（[MonoDesk](https://github.com/halcyonway/MonoDesk)） | **外部**（独立 App，ws 连 :8765） |

> 旧版本 `extensions/channels/monodesk/`（独立进程 ws server :8766）已废弃并删除。
> 桌面 UI 现在跟其他 channel 用同一套 ws 协议，更简单（少一层端口、多 session 共用更顺）。

## Layout

```
core/                 # stable kernel
├── protocol/         #   events + ws frame schema
├── loop/             #   ReAct engine + tool registry
├── llm_proxy/        #   OpenAI-compatible stream
├── sandbox/          #   bash exec
├── memory/           #   long-term memory (FsMemoryStore)
├── runtime_server.py #   ws server (:8765) — 唯一对外入口
├── session_manager.py #  multi-session + idle
├── health_server.py  #   HTTP :8767
└── config.py         #   config.toml loader
extensions/           # adapters (rewritable)
├── channels/         #   terminal / feishu / textual_chat (in-process RuntimeWSClient)
└── skills/           #   SKILL.md (LLM-readable)
run.py                # assembly entry
config.toml           # runtime config
spec/                 # design docs (ARCHITECTURE.md + requirements/)
```

## Test

```bash
uv run pytest tests/ -q
```

## Feature modules

| Feature | Spec |
|---|---|
| 1. 多 channel（独立进程 + 多 session 共享） | [`spec/requirements/multi-session.md`](spec/requirements/multi-session.md) |
| 2. 上下文压缩（L1/L2） | [`spec/requirements/context-compression.md`](spec/requirements/context-compression.md) |
| 3. 长期记忆（Memory.md + notes/） | [`spec/requirements/memory.md`](spec/requirements/memory.md) |
| 4. 事件建模（frozen dataclass + XML 包装） | [`spec/requirements/event-wrapper.md`](spec/requirements/event-wrapper.md) |