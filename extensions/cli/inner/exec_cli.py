#!/usr/bin/env python3
"""exec_cli — Extension CLI HTTP client.

A standalone executable that POSTs to the local CLI server. Installed by
`scripts/install.sh` to `~/.local/bin/exec_cli` so the agent (and humans)
can invoke extension CLIs uniformly:

    exec_cli mono_search "AI agent runtime" --count 5

This script is intentionally thin — argparse + urllib. No MonoX imports,
no third-party deps. Lives on the user's PATH; if the CLI server is down,
it fails fast with a clear error (rather than silently doing the wrong thing).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_SERVER = os.environ.get("EXEC_CLI_SERVER", "http://127.0.0.1:8769")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="exec_cli",
        description="MonoX extension CLI dispatcher (HTTP client to CLI server).",
    )
    p.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"CLI server base URL (default: {DEFAULT_SERVER}; env EXEC_CLI_SERVER).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds (default: 60).",
    )
    p.add_argument(
        "subcommand",
        help="Subcommand name (e.g. mono_search). Must be registered on the server.",
    )
    p.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to the subcommand handler.",
    )
    return p


def _post(server: str, subcommand: str, args: list[str], timeout: float) -> tuple[int, bytes]:
    url = f"{server.rstrip('/')}/cli/{subcommand}"
    body = json.dumps({"args": args}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    status, payload = _post(args.server, args.subcommand, args.args, args.timeout)
    sys.stdout.buffer.write(payload)
    if not payload.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")
    sys.stdout.flush()
    if status >= 400:
        # Print the body too on stderr for visibility — agents usually pipe stdout
        # into jq, so we keep stderr as a separate channel.
        sys.stderr.write(f"exec_cli: HTTP {status} from {args.server}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
