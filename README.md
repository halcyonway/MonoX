<div align="center">

<img src="logo.svg" alt="MonoX" width="280"/>

**English** · [中文](README_zh.md)

[![License](https://img.shields.io/github/license/halcyonway/MonoX?style=for-the-badge&color=1d4ed8)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-1d4ed8?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org)
[![WebSocket](https://img.shields.io/badge/WebSocket-Native-8b5cf6?style=for-the-badge&logo=socket.io&logoColor=white)](https://github.com/halcyonway/MonoX)

</div>

# MonoX

**A minimal, self-hosted Agent Runtime.** Modular by design — swap any component without touching the rest.

MonoX runs as a long-lived Python process speaking WebSocket. Channels, skills, and other harness extensions live in `extensions/` — completely decoupled from the core.

## Features

- **Protocol-first architecture** — Cross-module boundaries use `Protocol` + frozen dataclasses. Swap an implementation without changing anything else.
- **Multi-channel, multi-session** — One runtime, many channels. Sessions are isolated; share state via `last_active_source` fan-out.
- **Tool-augmented ReAct loop** — Extensible tool registry. Bash execution, memory, skills — all through the same protocol.
- **Hot-swappable extensions** — Anything in `extensions/` can be rewritten independently. Core stays untouched.
- **Long-term memory** — File-based storage with embedding-based retrieval.

## Architecture

```mermaid
flowchart TB
    subgraph Core["core / Stable Kernel"]
        Loop["LoopEngine<br/>(ReAct)"]
        Protocol["Protocol<br/>(events)"]
        LLMP["LLMProxy<br/>(stream)"]
        Sandbox["Sandbox<br/>(bash)"]
        Memory["Memory<br/>(FsMemory)"]
        Server["RuntimeServer<br/>:8765/ws"]
        Session["SessionManager<br/>(multi-sk)"]
        Health["HealthServer<br/>:8767"]
    end

    Loop & LLMP & Sandbox & Memory --> Protocol
    Server & Session & Health --> Protocol

    subgraph Extensions["extensions / Hot-swappable"]
        Channels["channels/<br/>terminal · feishu · textual · MonoDesk"]
        Skills["skills/<br/>SKILL.md (LLM-readable)"]
        Other["(any harness extension)"]
    end

    Server -->|"ws :8765"| Channels
    Skills -.-> Loop

    Loop --> Protocol
    LLMP --> Protocol
    Sandbox --> Protocol
    Memory --> Protocol
    Server --> Protocol
    Session --> Server
    Health --> Session

    style Core fill:#e8f2fc,stroke:#1d4ed8,color:#1e3a5f
    style Extensions fill:#f3effe,stroke:#6366f1,color:#4c1d95
    style Server fill:#e8faf0,stroke:#22c55e,color:#166534
    style Loop fill:#e8f0fd,stroke:#3b82f6,color:#1e3a5f
    style LLMP fill:#e8f0fd,stroke:#3b82f6,color:#1e3a5f
    style Sandbox fill:#e8f0fd,stroke:#3b82f6,color:#1e3a5f
    style Memory fill:#e8f0fd,stroke:#3b82f6,color:#1e3a5f
    style Session fill:#e8f0fd,stroke:#3b82f6,color:#1e3a5f
    style Health fill:#e8faf0,stroke:#22c55e,color:#166534
    style Protocol fill:#e8f0fd,stroke:#60a5fa,color:#1e3a5f
    style Channels fill:#f5f0ff,stroke:#8b5cf6,color:#4c1d95
    style Skills fill:#f5f0ff,stroke:#8b5cf6,color:#4c1d95
    style Other fill:#f5f0ff,stroke:#8b5cf6,color:#4c1d95
```

## Quick Start

```bash
# 1. Install dependencies
./scripts/install.sh

# 2. Configure
cp config.example.toml config.toml
# Edit config.toml: set api_base, api_key, model

# 3. Start the runtime
uv run python run.py          # ws server :8765, health :8767

# 4. Connect a channel
uv run python -m extensions.channels.terminal    # built-in terminal TUI

# Check health
curl http://127.0.0.1:8767/health
```

## Configuration

All settings in `config.toml`. Sensitive values use environment variable substitution:

| Variable | Description |
|---|---|
| `MINIMAX_API_KEY` | MiniMax API key |
| `ZHIPU_API_KEY` | Zhipu GLM API key |
| `FEISHU_APP_ID` | Feishu/Lark app ID |
| `FEISHU_APP_SECRET` | Feishu/Lark app secret |

## Project Layout

```
core/                    # Minimal runtime kernel — zero UI, zero extension SDK
├── protocol/            #   Event schemas (frozen dataclasses)
├── loop/                #   ReAct engine + tool registry
├── llm_proxy/           #   OpenAI-compatible streaming
├── sandbox/             #   Bash execution
├── memory/              #   Long-term memory (FsMemoryStore)
├── runtime_server.py    #   WebSocket server (:8765)
├── session_manager.py   #   Multi-session management
└── config.py            #   TOML config loader
extensions/              # Hot-swappable extensions
├── channels/            #   terminal / feishu / textual / MonoDesk
├── skills/              #   Skill definitions (LLM-readable)
└── ...                  #   Any harness extension can live here
run.py                   # Assembly entry point
config.toml              # Runtime configuration
```

## Channels

| Channel | Description |
|---|---|
| `terminal` | Stdio-based TUI |
| `feishu` | Feishu/Lark bot |
| `textual` | Textual full-screen TUI |
| `MonoDesk` | [Desktop UI](https://github.com/halcyonway/MonoDesk) |

## License

MIT
