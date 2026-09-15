"""Shared helpers for inner/ — JSON envelopes, error wrapping, trace logging."""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

_log = logging.getLogger("monox.cli.inner")


def ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a successful handler result. Keeps the original shape under `data`
    so callers can distinguish success from error envelopes without parsing strings."""
    return {"ok": True, "data": payload}


def err(message: str, **extra: Any) -> dict[str, Any]:
    """Wrap an error. `message` is the human-readable reason; `extra` carries
    structured details (status code, upstream error code, etc.)."""
    return {"ok": False, "error": {"message": message, **extra}}


def dump_json(obj: dict[str, Any]) -> bytes:
    """Serialize a response envelope to bytes (UTF-8, no ASCII escaping — Chinese OK)."""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def parse_request_body(raw: bytes) -> dict[str, Any]:
    """Decode JSON body. Empty body → {}. Malformed JSON → ValueError (caller 400s)."""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc


def log_call(subcommand: str, args: list[str], latency_ms: int, ok_flag: bool) -> None:
    """Single-line structured log for each CLI invocation. Goes to stderr so
    stdout stays clean for the JSON response."""
    status = "ok" if ok_flag else "err"
    _log.info("sub=%s status=%s latency_ms=%d args=%s",
              subcommand, status, latency_ms, args)
