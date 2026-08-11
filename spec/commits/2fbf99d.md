# 2fbf99d — initial: scaffold MonoX runtime

仓库初始骨架。建立了 `core/` / `extensions/` / `tests/` 三层目录结构，落地了最薄的 Channel → Gateway → Loop 协议管道：

- `core/protocol/`: Protocol 定义（Channel / LLMProxy / MemoryStore / SandboxRunner / 事件 dataclass）
- `core/channel/base.py`: Channel 协议占位
- `core/gateway/`: Gateway 占位（in_q / out_q pump 骨架）
- `core/loop/`: engine 占位（一轮 LLM 调用 + tool dispatch）
- `core/sandbox/`: BashRunner（subprocess）
- `core/llm_proxy/`: OpenAI-compatible stream httpx 调用
- `core/memory/`: FsMemoryStore（Memory.md + notes/）
- `run.py`: 顶层装配入口
- `tests/test_e2e.py`: 4 个 e2e 占位

无功能，仅可 import。下一 commit 实现完整 runtime。