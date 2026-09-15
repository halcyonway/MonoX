"""I2I extension CLI — `mono_i2i`."""
from extensions.cli.inner.registry import register


def _register() -> None:
    from extensions.cli.i2i.i2i import main
    register("mono_i2i")(main)


_register()
