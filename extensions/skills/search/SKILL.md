---
description: Bocha Web Search（中文搜索引擎，覆盖近 100 亿网页）。通过 exec_cli mono_search 调用。
tier: 1
---

# Search Skill — Bocha Web Search

调 `https://api.bochaai.com/v1/web-search`，返回 JSON。

## 调用方式

通过 `exec_cli` 调 CLI server（端口 8769，本机常驻）：

```sh
exec_cli mono_search "<query>" [--count N] [--freshness oneDay|oneWeek|oneMonth|oneYear|noLimit]
                              [--include domain.com] [--exclude spam.com]
```

## 参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `query` (positional) | str | 必填 | 搜索 query |
| `--count` | int | 10 | 返回结果数（1-50） |
| `--freshness` | str | `noLimit` | `oneDay` / `oneWeek` / `oneMonth` / `oneYear` / `noLimit` |
| `--include` | str | None | 只返回包含该域名的结果（如 `wikipedia.org`） |
| `--exclude` | str | None | 排除该域名的结果（如 `pinterest.com`） |

## API key

环境变量名是 **`BOCHA_API_KEY`**。请在 `~/.zshrc` 设置：

```sh
export BOCHA_API_KEY=sk-...
```

如果没设，CLI 返回 `{"ok":false,"error":{"message":"BOCHA_API_KEY not set",...}}`。

## CLI 输出格式

成功：

```json
{
  "ok": true,
  "data": {
    "query": "AI agent runtime",
    "count": 10,
    "provider": "bocha",
    "results": [
      {
        "title": "...",
        "url": "https://...",
        "summary": "...",
        "site": "...",
        "date": "2026-09-01"
      },
      ...
    ]
  }
}
```

LLM 拿到 `data.results[]` 直接读 `title` / `url` / `summary` / `date` / `site` 五个字段。

## 何时用

- 用户问"最新"/"今天"/"这周" 的事实性问题（新闻 / 价格 / 政策 / 天气）
- 用户给一段不熟悉的链接/关键词，要求做扩展研究
- agent 需要引用 web 源做答复

**不要**用于：纯本地代码搜索（用 ripgrep）、数学 / 逻辑 / 代码生成（用 LLM 自身）。

## 错误处理

| 现象 | 处理 |
|---|---|
| `BOCHA_API_KEY not set` | `export BOCHA_API_KEY=...` 后重试 |
| `bocha API failed: HTTP 401` | key 无效，去 bocha 控制台重新拿 |
| `bocha API failed: HTTP 429` | 限流，过会儿再试或换 query |
| `bocha API failed: ...` | 看 envelope `error.hint`；常见是网络瞬断，再调一次 |
| `exec_cli: cannot reach CLI server at ...` | 先 `uv run python -m extensions.cli.inner.server &` 起 server |