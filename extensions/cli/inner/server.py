"""Extension CLI HTTP server.

Routes POST /cli/<subcommand> → handler registered in `registry.py`.

Run standalone:
    python -m extensions.cli.inner.server [--host 127.0.0.1] [--port 8769]

Design notes:
- `http.server.ThreadingHTTPServer` is stdlib; we don't need async for short-lived
  sub-100ms CLI calls. Multi-thread so a slow upstream (Bocha) doesn't block the next call.
- All handlers must be idempotent & fast — no shared mutable state across calls.
- Response is always a JSON envelope: {"ok": bool, "data": ...} or {"ok": false, "error": ...}.
- Subcommand registration happens at import time in `bootstrap_builtins()` so any new
  module added under `extensions/cli/<name>/` just needs a one-liner here.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from extensions.cli.inner import common_util as cu
from extensions.cli.inner.registry import all_subcommands, get as get_handler

_log = logging.getLogger("monox.cli.server")


def bootstrap_builtins() -> None:
    """Import & register built-in subcommands. Each capability package's
    `__init__.py` registers itself via the registry as a side effect of import,
    so the server just needs to touch the packages."""
    import extensions.cli.search  # noqa: F401 — registers mono_search
    import extensions.cli.i2i  # noqa: F401 — registers mono_i2i
    import extensions.cli.asr  # noqa: F401 — registers mono_asr


class _Handler(BaseHTTPRequestHandler):
    """Single request handler — routes by path prefix `/cli/<subcommand>`.

    Response is always JSON. Error envelopes carry `{ok: false, error: {...}}`.
    """

    # Quieter logs — BaseHTTPRequestHandler prints every request to stderr by default.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        _log.debug(format, *args)

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        self._dispatch()

    def do_GET(self) -> None:  # noqa: N802
        # Cheap health check — also useful for `curl localhost:8769/cli/mono_search`
        # when a user forgets the POST.
        if self.path == "/healthz":
            self._write(HTTPStatus.OK, cu.ok({"subcommands": all_subcommands()}))
            return
        self._write(
            HTTPStatus.METHOD_NOT_ALLOWED,
            cu.err("use POST /cli/<subcommand>", method="POST", path=self.path),
        )

    # ---- internals ----

    def _dispatch(self) -> None:
        t0 = time.monotonic()
        if not self.path.startswith("/cli/"):
            self._write(HTTPStatus.NOT_FOUND, cu.err("not found", path=self.path))
            return
        subcommand = self.path[len("/cli/"):].strip("/")
        if not subcommand:
            self._write(HTTPStatus.BAD_REQUEST, cu.err("missing subcommand"))
            return
        handler = get_handler(subcommand)
        if handler is None:
            self._write(
                HTTPStatus.NOT_FOUND,
                cu.err("unknown subcommand", subcommand=subcommand,
                       available=all_subcommands()),
            )
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = cu.parse_request_body(raw)
        except ValueError as exc:
            self._write(HTTPStatus.BAD_REQUEST, cu.err(str(exc)))
            return
        args = body.get("args", []) if isinstance(body, dict) else []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            self._write(HTTPStatus.BAD_REQUEST,
                        cu.err("`args` must be a list[str]", got=type(args).__name__))
            return

        try:
            result = handler(args)
        except SystemExit as exc:
            # Handlers may sys.exit on bad usage; convert to a 400.
            code = exc.code if isinstance(exc.code, int) else 2
            self._write(
                HTTPStatus.BAD_REQUEST if code == 2 else HTTPStatus.INTERNAL_SERVER_ERROR,
                cu.err("handler exited", code=code),
            )
            cu.log_call(subcommand, args,
                        int((time.monotonic() - t0) * 1000), ok_flag=False)
            return
        except Exception as exc:  # last-resort: don't crash the server on a handler bug
            _log.exception("handler %r raised", subcommand)
            self._write(HTTPStatus.INTERNAL_SERVER_ERROR,
                        cu.err(f"{type(exc).__name__}: {exc}"))
            cu.log_call(subcommand, args,
                        int((time.monotonic() - t0) * 1000), ok_flag=False)
            return

        # Handlers may return either an envelope (preferred) or a raw dict
        # (treated as `ok=True data=<dict>` for ergonomics).
        if isinstance(result, dict) and "ok" in result:
            envelope = result
        elif isinstance(result, dict):
            envelope = cu.ok(result)
        else:
            self._write(HTTPStatus.INTERNAL_SERVER_ERROR,
                        cu.err("handler must return dict", got=type(result).__name__))
            return

        self._write(HTTPStatus.OK, envelope)
        cu.log_call(subcommand, args,
                    int((time.monotonic() - t0) * 1000),
                    ok_flag=envelope.get("ok", False))

    def _write(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = cu.dump_json(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class CliServer:
    """Blocking HTTP server. Run in a thread / subprocess; lifecycle is caller's job."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8769) -> None:
        self._host = host
        self._port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else self._port

    def run(self) -> None:
        bootstrap_builtins()
        self._httpd = ThreadingHTTPServer((self._host, self._port), _Handler)
        _log.info("cli-server listening on http://%s:%d (subcommands=%s)",
                  self._host, self._httpd.server_address[1], all_subcommands())
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            _log.info("cli-server shutting down")
        finally:
            self._httpd.server_close()

    def run_in_thread(self) -> threading.Thread:
        """For tests / in-process embedding. Caller is responsible for `shutdown()`."""
        bootstrap_builtins()
        self._httpd = ThreadingHTTPServer((self._host, self._port), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        _log.info("cli-server thread listening on http://%s:%d",
                  self._host, self._httpd.server_address[1])
        return self._thread

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [monox-cli:%(name)s] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    p = argparse.ArgumentParser(prog="extensions.cli.inner.server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8769)
    args = p.parse_args()
    CliServer(host=args.host, port=args.port).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
