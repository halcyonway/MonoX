"""Bocha Web Search — extension CLI subcommand.

Importing this package registers `mono_search` with the inner registry as a
side effect. `server.bootstrap_builtins()` triggers this.
"""
from extensions.cli.inner.registry import register


def _register() -> None:
    # Late import: search.py imports common_util; importing at module top would
    # pull the whole graph at package-init time, which we want to avoid for tests
    # that only need the registry without the Bocha dep.
    from extensions.cli.search.search import main
    register("mono_search")(main)


_register()
