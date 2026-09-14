---
description: Bocha Web Search — 中文 + 全球网页搜索（覆盖近 100 亿网页、新闻、百科、问答）。通过 exec_cli mono_search 调用。
tier: 1
---

# Search Skill — Bocha Web Search

需要联网搜索事实、新闻、技术细节、产品信息时使用本 skill。

## 调用方式

通过 `exec_cli` 调用 CLI server（端口 8769，本机常驻）：

```sh
exec_cli mono_search "<query>" [--count N] [--freshness oneDay|oneWeek|oneMonth|oneYear|noLimit] [--summary|--no-summary] [--include domain.com|...] [--exclude spam.com|...]
```

参数：

- `query`（必填）— 搜索关键词
- `--count` — 返回条数，1–50，默认 10
- `--freshness` — 时间过滤；除 `oneDay/oneWeek/oneMonth/oneYear/noLimit` 外还支持 `YYYY-MM-DD` 或 `YYYY-MM-DD..YYYY-MM-DD`
- `--include` — 只搜指定域名（`|` 或 `,` 分隔），如 `arxiv.org|github.com`
- `--exclude` — 排除指定域名
- `--summary` / `--no-summary` — 是否包含文本摘要（默认开）

## 输出格式

`exec_cli` 把 CLI server 的 JSON envelope 原样打到 stdout。成功：

```json
{
  "ok": true,
  "data": {
    "query": "AI agent runtime",
    "count": 3,
    "provider": "bocha",
    "results": [
      {"title": "...", "url": "https://...", "summary": "...", "site": "...", "date": "2026-..."},
      ...
    ]
  }
}
```

失败（API key 没设、网络挂、参数错等）：

```json
{"ok": false, "error": {"message": "BOCHA_API_KEY not set", "hint": "..."}}
```

## 环境变量

`BOCHA_API_KEY`（必填）— 在启动 agent 的 shell 里 export，CLI server 子进程继承。

```sh
# ~/.zshrc 或运行时 shell
export BOCHA_API_KEY=<your-key>
```

## 工作流（agent 调用）

1. **判断要不要搜** — 用户问"最近 / 2026 / 最新 / 现在 / 刚刚"之类带时效的，或问具体事实、产品、技术细节，先搜。
2. **执行** — `exec_cli mono_search "<query>" --count 8` 拿到结果。
3. **精读** — LLM 直接基于 `results[].summary` 回答用户；如要原文用 `url` 让 user 自己看或后续用 bash 抓。
4. **复合查询** — 一次搜不够，多发几次并行 / 串行（不同 query 角度）。
5. **不要**自己 `python -c` 调 Bocha API — 统一走 `exec_cli`，让 server 做隔离和日志。

## 何时用 / 何时不用

**用：**
- 用户问"X 是什么 / 最新 / 怎么样 / 对比"
- 实时性内容（新闻、价格、股票、天气）
- 引用具体来源（论文、博客、文档）

**不用：**
- 本地文件系统 / git / 代码库问题 — 用 bash + skill_load
- 私域知识（用户 Memory.md） — cat memory
- 数学 / 推理 — 纯 LLM 即可

## 错误处理

| 现象 | 处理 |
|---|---|
| `BOCHA_API_KEY not set` | 提示用户在 shell 里 export 后重试 |
| `HTTP 401` | key 无效，去博查开放平台重新生成 |
| `HTTP 429` | 限流，等几秒再试；持续 429 说明 quota 用完 |
| `CLI server unreachable` | 提醒用户启动 `python -m extensions.cli.inner.server` |
| `unknown subcommand` | 检查 `exec_cli --help` 和 server 是否最新版本 |

## 后续扩展（不在本期）

- 多 provider（Tavily / Bing）— `mono_search --provider=...`
- 多路召回 + rerank — 输出仍是 `results[]`，LLM 无感
- 本地缓存 — 相同 query 命中 `.monox/state/search-cache/`，省 quota
