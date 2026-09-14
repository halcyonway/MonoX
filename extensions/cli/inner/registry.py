"""Subcommand registry — maps `mono_*` route names to handler callables.

Handlers are registered via the `@register("mono_xxx")` decorator and must accept
`args: list[str]` (already-parsed argv tail) and return a JSON-serializable dict.
"""
from __future__ import annotations

from typing import Callable

Handler = Callable[[list[str]], dict]

_REGISTRY: dict[str, Handler] = {}


def register(subcommand: str) -> Callable[[Handler], Handler]:
    """Decorator: register `mono_<name>` → handler.

    Idempotent: re-registering the same name overwrites (useful for hot-reload).
    """
    def deco(fn: Handler) -> Handler:
        _REGISTRY[subcommand] = fn
        return fn
    return deco


def get(subcommand: str) -> Handler | None:
    return _REGISTRY.get(subcommand)


def all_subcommands() -> list[str]:
    return sorted(_REGISTRY.keys())
