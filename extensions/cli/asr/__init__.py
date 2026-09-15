"""ASR extension CLI — `mono_asr`."""
from extensions.cli.inner.registry import register


def _register() -> None:
    from extensions.cli.asr.asr import main
    register("mono_asr")(main)


_register()
