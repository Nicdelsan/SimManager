"""Built-in plugins. They use exactly the same API as external (entry point) plugins."""

from __future__ import annotations

from simmanager.kernel import Plugin

from .example import ExampleClockPlugin, ExampleTickerPlugin
from .storage_sqlite import SqliteStoragePlugin

BUILTIN_PLUGINS: tuple[type[Plugin], ...] = (
    SqliteStoragePlugin,
    ExampleClockPlugin,
    ExampleTickerPlugin,
)

DISABLED_BY_DEFAULT: frozenset[str] = frozenset({"example-clock", "example-ticker"})
