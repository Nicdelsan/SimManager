"""Built-in plugins. They use exactly the same API as external (entry point) plugins."""

from __future__ import annotations

from simmanager.kernel import Plugin

from .example import ExampleClockPlugin, ExampleTickerPlugin
from .monitor import MonitorPlugin
from .prompter_cli import CliPrompterPlugin
from .scheduler_slurm import SlurmSchedulerPlugin
from .solver_converge import ConvergeSolverPlugin
from .solver_ingest import SolverIngestPlugin
from .solver_openfoam import OpenFoamSolverPlugin
from .storage_sqlite import SqliteStoragePlugin, SqliteTimeSeriesPlugin
from .transport_ssh import SshTransportPlugin
from .ui_web import UiWebPlugin

BUILTIN_PLUGINS: tuple[type[Plugin], ...] = (
    SqliteStoragePlugin,
    SqliteTimeSeriesPlugin,
    CliPrompterPlugin,
    SshTransportPlugin,
    SlurmSchedulerPlugin,
    MonitorPlugin,
    ConvergeSolverPlugin,
    OpenFoamSolverPlugin,
    SolverIngestPlugin,
    UiWebPlugin,
    ExampleClockPlugin,
    ExampleTickerPlugin,
)

DISABLED_BY_DEFAULT: frozenset[str] = frozenset({"example-clock", "example-ticker"})

WEB_ONLY_PLUGINS: frozenset[str] = frozenset({"ui-web"})
"""Started only by `simman serve`: other commands must not open a web server."""
