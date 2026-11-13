"""Built-in plugins. They use exactly the same API as external (entry point) plugins."""

from __future__ import annotations

from simmanager.kernel import Plugin

from .campaign_excel import CampaignExcelPlugin
from .example import ExampleClockPlugin, ExampleTickerPlugin
from .monitor import MonitorPlugin
from .prompter_cli import CliPrompterPlugin
from .scheduler_slurm import SlurmSchedulerPlugin
from .solver_converge import ConvergeSolverPlugin
from .solver_ingest import SolverIngestPlugin
from .solver_openfoam import OpenFoamSolverPlugin
from .storage_sqlite import (
    SqliteCampaignPlugin,
    SqliteStoragePlugin,
    SqliteTimeSeriesPlugin,
)
from .transport_ssh import SshTransportPlugin

BUILTIN_PLUGINS: tuple[type[Plugin], ...] = (
    SqliteStoragePlugin,
    SqliteTimeSeriesPlugin,
    SqliteCampaignPlugin,
    CampaignExcelPlugin,
    CliPrompterPlugin,
    SshTransportPlugin,
    SlurmSchedulerPlugin,
    MonitorPlugin,
    ConvergeSolverPlugin,
    OpenFoamSolverPlugin,
    SolverIngestPlugin,
    ExampleClockPlugin,
    ExampleTickerPlugin,
)

DISABLED_BY_DEFAULT: frozenset[str] = frozenset({"example-clock", "example-ticker"})
