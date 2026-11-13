"""Built-in plugins. They use exactly the same API as external (entry point) plugins."""

from __future__ import annotations

from simmanager.kernel import Plugin

from .example import ExampleClockPlugin, ExampleTickerPlugin
from .solver_converge import ConvergeSolverPlugin
from .solver_ingest import SolverIngestPlugin
from .solver_openfoam import OpenFoamSolverPlugin
from .storage_sqlite import SqliteStoragePlugin, SqliteTimeSeriesPlugin

BUILTIN_PLUGINS: tuple[type[Plugin], ...] = (
    SqliteStoragePlugin,
    SqliteTimeSeriesPlugin,
    ConvergeSolverPlugin,
    OpenFoamSolverPlugin,
    SolverIngestPlugin,
    ExampleClockPlugin,
    ExampleTickerPlugin,
)

DISABLED_BY_DEFAULT: frozenset[str] = frozenset({"example-clock", "example-ticker"})
