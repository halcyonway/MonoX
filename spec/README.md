# MonoX spec

仓库设计 / 决策 / 进展的可追溯记录。

## 目录结构

```
spec/
├── README.md          # 本文件（导航）
├── rule.md            # 项目规则：架构分层 + 注释原则
├── goal.md            # 长期目标
├── requirements/      # 规划中尚未实现的需求方案
│   ├── shutdown.md    # engine 响应 channel 关闭
│   ├── context-compression.md   # L0/L1/L2 上下文压缩
│   ├── memory.md      # 长期记忆 / Memory.md 维护策略
│   ├── hitl.md        # 敏感操作人工确认
│   ├── feishu-channel.md
│   └── llm-harness.md # LLMProxy retry/fallback/rate-limit
└── commits/           # 每个 git commit 的总结（按 SHA 划分）
    ├── 2fbf99d.md     # initial scaffold
    ├── a4e817a.md     # v0.1 full runtime
    └── ...
```

## 怎么用

- **改 core 前**：先看 `rule.md`，确认这次改动确实属于 core 的稳定 API，不只是 adapter 的事
- **新需求**：先在 `requirements/` 起一份方案文档；实现时再写 commits/ 对应的 commit summary
- **追溯某次改动**：`git log` 找 SHA，然后 `cat spec/commits/<sha>.md`

## 历史 spec 文件

`2026-08-10-impl-v0.{1..9}.md` 与 `2026-08-10-initial-design.md` 已被迁移到
`commits/`（按 SHA 粒度更细）。旧文件删除以避免双份维护。