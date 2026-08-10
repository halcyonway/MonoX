# MonoX

极简 Agent Runtime Core。通过 IM（飞书 / Slack / 本地 terminal）与 Agent 交互，运行在本地 docker 中。

## 架构

```
Channel → Gateway → Loop → {Sandbox, LLMProxy}
                  → Memory
```

完整设计见 `Project/Agent Runtime Core - 设计方案.md` 或 `spec/` 下的迭代记录。

## 目录

- `core/` — 最小 runtime 核心（Python）
- `extensions/` — 业务能力（skill / cli，独立迭代）
- `spec/` — 每次需求的设计讨论与 AI coding 记录

## 依赖管理（uv）

MonoX 用 [uv](https://docs.astral.sh/uv/) 管理所有依赖。lock 文件 `uv.lock` 入库。

```bash
# 装依赖到本地 .venv
uv sync

# 加 runtime 依赖
uv add httpx

# 加 dev 依赖（pytest 等）
uv add --dev pytest

# 跑测试
uv run python tests/test_e2e.py

# 跑 agent
uv run python -m core.main config.toml
```

## 本地启动

```bash
# 1. 改 sandbox 路径为本地（参考 config.example.toml）
# 2. 准备目录
mkdir -p /tmp/monox/skills
cp -r extensions/skills/memory-write /tmp/monox/skills/

# 3. 注入 API key
export OPENAI_API_KEY=sk-xxx

# 4. 跑
uv run python -m core.main config.toml
```

## Docker

```bash
docker compose up
```

容器内目录约定：
```
/etc/agent/config.toml
/var/agent/workspace/<session_key>/
/var/agent/memory/<session_key>/
/var/agent/skills/        # 共享，不按 session 隔离
/var/agent/tmp/
```