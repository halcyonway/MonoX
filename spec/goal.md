# 长期目标

MonoX 是一个**自托管最小 agent runtime core**。目标不是产品化 SaaS，而是一个干净的、可重写的、和云端 SaaS 解耦的代码骨架。

## 一句话目标

> 让一个程序员能在周末 fork MonoX，5 分钟接入他手头的 LLM，跑通本地 coding agent；想替换任何部分（channel / LLM / 工具 / 存储）都能在 `extensions/` 里独立完成，不动 `core/`。

## 设计哲学

1. **极致解耦**：core 不知道 channel、UI、IM、第三方 LLM SDK 存在。core 是一个可独立测试的协议引擎。
2. **小而稳定**：core 不追求 feature 数量，只追求接口稳定。任何新需求先想「这是 core 的事还是 extension 的事」。
3. **依赖最小**：避免「feature 强但依赖重」的库（如 LangChain、autogen）。需要时手写，不超过 100 行。
4. **可观测**：每次 turn 都能看到 step / latency / token / state 切换（debug mode / spec metrics）。
5. **可中断**：Ctrl+C、exit 都能让 runtime 干净退出（待解决）。

## 短期（v1.0 前）

- `core/` 稳定
- channel：terminal（Rich + prompt_toolkit）✅ + textual ✅
- skill 加载机制 ✅
- 持久对话历史 ✅（checkpoint）
- 记忆（Memory.md）✅ 加载，但**自动摘要 / 维护** 待做
- L1/L2/L3 上下文压缩（接口已有，未启用）

## 中期

- feishu channel adapter（让 IM 也能用）
- shutdown_event（channel 关闭让 engine 干净退出）
- bash tool dangerous_patterns HITL hook
- LLMProxy harness（retry / fallback / rate-limit）

## 长期

- 跨 session 知识沉淀（Memory.md 自动摘要 + recall）
- 第三方 skill 注册中心（不动 core，extensions/skills/ + spec/）
- 嵌入式部署（无 terminal 场景，跑后台服务）

## 不做的事

- ❌ 内置 web UI / 浏览器前端（推 user 用 Textual 改写）
- ❌ 多 agent orchestration（单 loop 即可，避免复杂度爆炸）
- ❌ 商业计费 / quota / 多租户（runtime core 不管）
- ❌ 兼容 py<3.10（pyproject 已写 `>=3.10`，旧 Python 用户用更早 tag）
- ❌ 兼容 Windows shell（bash 为唯一 shell）

## 衡量标准

什么时候算「v1.0」：
- ✅ core 不再需要功能改动
- ✅ 3 个 channel 都 work（terminal / textual / feishu）
- ✅ 2 个 skill（coding + memory-write）
- ✅ e2e + smoke test 覆盖核心路径
- ✅ 一个真实项目（用 MonoX 写 MonoX 的某一版本）成功跑通

什么时候算「failed project」：
- ❌ core 越来越胖，开始引用 UI / LLM SDK
- ❌ extensions/ 改动需要同步改 core
- ❌ e2e 跑 3 分钟还没完
- ❌ 同一需求在 3 个 channel 下行为不一致