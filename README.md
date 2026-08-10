# MonoX

极简 Agent Runtime Core。通过 IM（飞书 / Slack / 本地 terminal）与 Agent 交互，运行在本地 docker 中。

## 架构

```
Channel → Gateway → Loop → {Sandbox, LLMProxy}
                  → Memory
```

完整设计见 [`Project/Agent Runtime Core - 设计方案.md`](../) 或 `spec/` 下的迭代记录。

## 目录

- `core/` — 最小 runtime 核心（Python）
- `extensions/` — 业务能力（skill / cli，独立迭代）
- `spec/` — 每次需求的设计讨论与 AI coding 记录

## 启动

```bash
docker compose up
```

## 开发

```bash
uv venv
uv pip install -e .
python -m core.main
```