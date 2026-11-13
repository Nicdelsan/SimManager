"""Built-in plugins. They use exactly the same API as external (entry point) plugins."""

from __future__ import annotations

from simmanager.kernel import Plugin

from .example import ExampleClockPlugin, ExampleTickerPlugin
from .monitor import MonitorPlugin
from .prompter_cli import CliPrompterPlugin
from .scheduler_slurm import SlurmSchedulerPlugin
from .storage_sqlite import SqliteStoragePlugin
from .transport_ssh import SshTransportPlugin

BUILTIN_PLUGINS: tuple[type[Plugin], ...] = (
    SqliteStoragePlugin,
    CliPrompterPlugin,
    SshTransportPlugin,
    SlurmSchedulerPlugin,
    MonitorPlugin,
    ExampleClockPlugin,
    ExampleTickerPlugin,
)

DISABLED_BY_DEFAULT: frozenset[str] = frozenset({"example-clock", "example-ticker"})
