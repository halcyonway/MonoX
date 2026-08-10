# MonoX

极简 Agent Runtime Core。通过 IM（飞书 / Slack / 本地 terminal）与 Agent 交互，运行在本地 docker 中。

## 架构

```
Channel → Gateway → Loop → {Sandbox, LLMProxy}
                  → Memory
```

完整设计见 `Project/Agent Runtime Core - 设计方案.md` 或 `spec/` 下的迭代记录。

## 目录

- `core/` — 最小 runtime 核心（Python，稳定内核）
- `extensions/` — 业务能力（skill / cli，独立迭代）
- `run.py` — 启动入口（装配 + 启动）
- `scripts/install.sh` — 一次性本地准备
- `spec/` — 每次需求的设计讨论与 AI coding 记录

## 依赖管理（uv）

```bash
uv sync              # 装所有依赖到 .venv
uv add <pkg>         # 加 runtime 依赖
uv add --dev <pkg>   # 加 dev 依赖
uv run python ...    # 用 .venv 跑
```

## 本地启动

```bash
# 1. 一次性准备（建 .monox + 拷默认 skill）
./scripts/install.sh

# 2. 配 API key
export MINIMAX_API_KEY=eyJ...   # 或 OPENAI_API_KEY / DEEPSEEK_API_KEY 等

# 3. 改 config.toml：api_base / api_key / model 对应你的 provider
#    默认值已指向 api.minimaxi.com + MiniMax-M2.7

# 4. 跑
uv run python run.py
```

启动后看到 `[state: thinking]`，输入消息，agent 开始工作。`exit` 或 Ctrl+C 退出。

## 数据目录（默认 `.monox/`）

```
.monox/
├── workspace/<session_key>/    # agent 工作目录
├── memory/<session_key>/       # Memory.md + notes/ + checkpoint.jsonl
├── skills/                     # 共享 skill（install.sh 拷默认）
└── tmp/                        # 临时文件
```

所有路径在 config.toml `[sandbox]` 段可改。

## Docker（未来多种 image）

当前 image 是 agent loop + bash 沙箱。未来可能拆：
- `monox-runtime`：纯 runtime（sqlite 等）
- `monox-agent`：loop + sandbox
- `monox-gateway`：IM 适配层

具体先不设计，先本地跑通。

## 测试

```bash
uv run python tests/test_e2e.py
```

覆盖：
- `test_basic_bash`：基础 bash + final message
- `test_wait_io_ends_turn`：wait_io 主动结束（1 次 LLM 调用）
- `test_queue_aggregate_continues_react`：queue 聚合继续 react