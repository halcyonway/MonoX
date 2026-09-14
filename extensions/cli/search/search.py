"""Bocha Web Search CLI — `mono_search`.

Calls Bocha's `/v1/web-search` endpoint and normalizes the response into a
flat JSON shape that the agent can pipe into downstream tools / display.

Auth: `BOCHA_API_KEY` env var (Bearer). No key → envelope `{ok:false, error:...}`,
not an exception, so the agent gets a clean signal and can ask the user for setup.

Output schema:
    {
      "ok": true,
      "data": {
        "query": "<echo>",
        "count": <int>,
        "provider": "bocha",
        "results": [
          {"title": ..., "url": ..., "summary": ..., "site": ..., "date": ...},
          ...
        ]
      }
    }

Future-proofing:
- `provider` field lets a later multi-provider dispatcher (Tavily / Bing) coexist
  with Bocha results merged under the same shape.
- `results[]` is a flat list — when multi-recall + rerank lands, the merger
  upstream will populate this list; the schema doesn't change.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from extensions.cli.inner import common_util as cu

_log = logging.getLogger("monox.cli.search")

BOCHA_URL = "https://api.bochaai.com/v1/web-search"
DEFAULT_COUNT = 10
MAX_COUNT = 50


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mono_search",
        description="Bocha Web Search — Chinese + global web search.",
    )
    p.add_argument("query", help="Search query string.")
    p.add_argument(
        "--count", type=int, default=DEFAULT_COUNT,
        help=f"Results per page (1-{MAX_COUNT}, default {DEFAULT_COUNT}).",
    )
    p.add_argument(
        "--freshness", default="noLimit",
        help="Time range: noLimit | oneDay | oneWeek | oneMonth | oneYear | YYYY-MM-DD | YYYY-MM-DD..YYYY-MM-DD.",
    )
    p.add_argument(
        "--summary", action=argparse.BooleanOptionalAction, default=True,
        help="Include text summaries (default: --summary).",
    )
    p.add_argument(
        "--include", default=None,
        help="Comma-separated domains to include (e.g. 'arxiv.org|github.com').",
    )
    p.add_argument(
        "--exclude", default=None,
        help="Comma-separated domains to exclude.",
    )
    return p


def _call_bocha(api_key: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """POST to Bocha; raises urllib errors on transport failure.

    Returns the parsed JSON body. We don't do schema validation here — Bocha's
    field shape is captured in `_normalize_results` and silently-skipped on miss.
    """
    req = urllib.request.Request(
        BOCHA_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize_results(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Bocha response shape: `data.webPages.value[]`. We flatten to a stable list
    so downstream tools don't break if Bocha adds new wrapper layers."""
    web_pages = raw.get("data", {}).get("webPages", {}).get("value", []) or []
    out: list[dict[str, Any]] = []
    for item in web_pages:
        out.append({
            "title": item.get("name"),
            "url": item.get("url"),
            "summary": item.get("summary"),
            "site": item.get("siteName"),
            "date": item.get("datePublished"),
            "icon": item.get("siteIcon"),
        })
    return out


def main(args: list[str]) -> dict[str, Any]:
    """Registry handler entry point. Receives argv tail, returns envelope."""
    parsed = _build_parser().parse_args(args)

    if not parsed.query.strip():
        return cu.err("query must not be empty")
    if not (1 <= parsed.count <= MAX_COUNT):
        return cu.err("--count out of range",
                      min=1, max=MAX_COUNT, got=parsed.count)

    api_key = os.environ.get("BOCHA_API_KEY", "").strip()
    if not api_key:
        return cu.err(
            "BOCHA_API_KEY not set",
            hint="export BOCHA_API_KEY=<your-key> before invoking mono_search",
        )

    payload: dict[str, Any] = {
        "query": parsed.query,
        "freshness": parsed.freshness,
        "summary": parsed.summary,
        "count": parsed.count,
    }
    if parsed.include:
        payload["include"] = parsed.include
    if parsed.exclude:
        payload["exclude"] = parsed.exclude

    try:
        raw = _call_bocha(api_key, payload)
    except urllib.error.HTTPError as exc:
        # Bocha returns errors as JSON bodies with `code`/`msg`; surface them.
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        _log.warning("bocha HTTP %d: %s", exc.code, body)
        return cu.err(f"bocha API HTTP {exc.code}",
                      upstream_code=exc.code, upstream_body=body[:500])
    except urllib.error.URLError as exc:
        return cu.err(f"bocha API unreachable: {exc.reason}")
    except json.JSONDecodeError as exc:
        return cu.err(f"bocha returned non-JSON: {exc}")
    except TimeoutError:
        return cu.err("bocha API timed out", timeout_sec=30)

    results = _normalize_results(raw)
    return cu.ok({
        "query": parsed.query,
        "count": len(results),
        "provider": "bocha",
        "results": results,
    })


if __name__ == "__main__":
    """Standalone usage: `python -m extensions.cli.search "query" --count 5`.

    Bypasses the HTTP server; useful for debugging / cron jobs."""
    sys.stdout.write(json.dumps(main(sys.argv[1:]), ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
