# MonoX spec

仓库设计 / 决策 / 进展的可追溯记录。

## 文档导航

| 文档 | 内容 |
|---|---|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 权威架构设计：模块 + 协议 + 依赖方向 |
| [rule.md](./rule.md) | 开发规范 + 踩坑记录 |
| [goal.md](./goal.md) | 长期目标 |
| [requirements/](./requirements/) | 尚未实现的需求方案 |

## 快速入口

- **新需求**：看 `requirements/` 下的需求文档，了解设计背景和方案
- **改 core 前**：先看 `rule.md`，确认是否属于 core 稳定 API
- **了解 MonoX**：先读 [ARCHITECTURE.md](./ARCHITECTURE.md)
- **Runtime 多 session + Channel 配置驱动启动 + `/health`**：看 [ARCHITECTURE.md §12](./ARCHITECTURE.md#12-runtime--channel-in-process--配置驱动启动) + [requirements/multi-session.md](./requirements/multi-session.md)
- **追溯改动**：`git log` 找 SHA，然后 `cat spec/commits/<sha>.md`

## 目录结构

```
spec/
├── README.md              # 本文件（导航）
├── ARCHITECTURE.md        # 权威架构设计：模块 + 协议 + 依赖方向
│                          # §12 Runtime + Channel 独立进程化（核心章节）
├── rule.md                # 项目规则：架构分层 + 注释原则
├── goal.md                # 长期目标
├── requirements/          # 规划中尚未实现的需求方案
│   ├── event-wrapper.md      # 外部信号统一包装协议
│   ├── multi-channel.md      # 多通道并行接入（旧 in-process 方案，已迁移到 ARCHITECTURE §12）
│   ├── multi-session.md      # Runtime 多 session + Channel 独立进程化（已实现）
│   ├── memory.md             # 跨会话长期记忆（已实现）
│   ├── feishu-channel.md     # 飞书接入
│   ├── context-compression.md # L0/L1/L2 上下文压缩
│   ├── shutdown.md           # engine 响应 channel 关闭
│   ├── interrupt.md          # LoopEngine 运行中打断（独立中断队列 + 最高优先级）
│   ├── runtime-lifecycle.md  # PID 文件 / --stop / channel supervisor（操作员级进程管理）
│   └── llm-harness.md       # LLMProxy retry/fallback/rate-limit
└── commits/               # 每个 git commit 的总结（按 SHA 划分）
```

## 历史 spec 文件

`2026-08-10-impl-v0.{1..9}.md` 与 `2026-08-10-initial-design.md` 已被迁移到
`commits/`（按 SHA 粒度更细）。旧文件删除以避免双份维护。
