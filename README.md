# MonoX

自托管的 Agent Runtime Core。

**系统 = 模块 + 协议。** Runtime 进程跑核心（协议 + ReAct 引擎 + 存储/执行抽象），channel 作为 feature 通过协议接入。

[MonoDesk](https://github.com/halcyonway/MonoDesk) 是 monoDesk 桌面 UI 的 channel 实现（独立仓库）。

## 设计哲学

- **核心稳定，适配可换** —— `core/` 是 zero UI / zero IM / zero LLM SDK 的稳定内核；`extensions/` 是可重写的 adapter 集合；`run.py` 是装配层
- **协议优先** —— 跨模块用 `Protocol` + frozen dataclass，鸭子类型；替换实现不动协议，其他模块零修改
- **Runtime 不感知 channel** —— RuntimeServer 只见 ws 帧；channel 类型 / 协议对它透明
- **单进程多 session** —— 多 channel 共享主 session（last_active_source fan-out），或各自独立 session_key
- **配置驱动** —— `config.toml` 管所有连接参数，runtime.py 只读 LLM / server / sandbox 三段

完整设计：`spec/ARCHITECTURE.md`（§2 总览 / §6 core 模块 / §9 extensions 细化 / §12 多 session）。

## 架构

```mermaid
graph TB
    subgraph Core["core/ (稳定内核)"]
        Protocol["protocol/<br/>事件 + 数据结构契约"]
        Loop["loop/<br/>ReAct 引擎 + tools"]
        LLMP["llm_proxy/<br/>OpenAI 流式"]
        Sandbox["sandbox/<br/>bash 执行"]
        Memory["memory/<br/>长期记忆"]
        Server["runtime_server/<br/>ws server"]
        Session["session_manager/<br/>多 session + idle"]
        Health["health_server/<br/>:8767"]
    end

    subgraph Extensions["extensions/ (可换 adapter)"]
        Channels["channels/<br/>terminal / monodesk / feishu / textual_chat"]
        Skills["skills/<br/>memory-write / ..."]
    end

    Run["run.py<br/>装配入口"]

    Loop --> Protocol
    LLMP --> Protocol
    Sandbox --> Protocol
    Memory --> Protocol
    Server --> Protocol
    Session --> Server
    Health --> Session

    Channels -.实现 Channel 协议.-> Protocol
    Skills -.LLM 读 SKILL.md.-> Loop

    Run --> Core
    Run --> Channels

    classDef core fill:#e8f4f8,stroke:#333,stroke-width:2px
    classDef ext fill:#fdf3e7,stroke:#333,stroke-width:1px
    classDef run fill:#f0f0f0,stroke:#555
    class Protocol,Loop,LLMP,Sandbox,Memory,Server,Session,Health core
    class Channels,Skills ext
    class Run run
```

## 快速启动

```bash
# 1. 准备
./scripts/install.sh

# 2. 配置
export MINIMAX_API_KEY=eyJ...
# 编辑 config.toml：api_base / api_key / model

# 3. 启动 Runtime
uv run python run.py

# 4. 连 channel（独立进程，可多个并发）
uv run python -m extensions.channels.terminal   # terminal TUI
# MonoDesk 桌面端：https://github.com/halcyonway/MonoDesk

# 5. 查 Runtime 状态
curl http://127.0.0.1:8767/health
```

## Channel 列表

| Channel | 仓库 | 启动 | 说明 |
|---|---|---|---|
| terminal | MonoX | `uv run python -m extensions.channels.terminal` | stdio TUI |
| monodesk | [MonoDesk](https://github.com/halcyonway/MonoDesk) | `npm run tauri dev` | 桌面 app |
| feishu | MonoX | `uv run python -m extensions.channels.feishu` | 飞书 lark-oapi |
| textual | MonoX | `uv run python -m extensions.channels.textual_chat` | textual 全屏 TUI |

## 目录结构

```
core/                 # 稳定内核
├── protocol/         #   事件契约 + ws 帧格式
├── loop/             #   ReAct 引擎 + tool 注册
├── llm_proxy/        #   OpenAI-compatible 流式
├── sandbox/          #   bash 执行
├── memory/           #   长期记忆 (FsMemoryStore)
├── runtime_server.py #   ws server (:8765)
├── session_manager.py #  多 session + idle 销毁
├── health_server.py  #   HTTP :8767
└── config.py         #   config.toml 加载
extensions/           # adapter 层（可重写）
├── channels/         #   terminal / monodesk / feishu / textual_chat
└── skills/           #   SKILL.md（LLM 读）
run.py                # 装配入口
config.toml           # 运行时配置
spec/                 # 设计文档（ARCHITECTURE.md + requirements/）
```

## 测试

```bash
uv run pytest tests/ -q
```