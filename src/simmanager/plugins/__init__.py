"""Built-in plugins. They use exactly the same API as external (entry point) plugins."""

from __future__ import annotations

from simmanager.kernel import Plugin

from .campaign_excel import CampaignExcelPlugin
from .example import ExampleClockPlugin, ExampleTickerPlugin
from .monitor import MonitorPlugin
from .prompter_cli import CliPrompterPlugin
from .scheduler_slurm import SlurmSchedulerPlugin
from .snapshot import SnapshotPlugin
from .solver_converge import ConvergeSolverPlugin
from .solver_ingest import SolverIngestPlugin
from .solver_openfoam import OpenFoamSolverPlugin
from .storage_sqlite import (
    SqliteCampaignPlugin,
    SqliteRulePlugin,
    SqliteSnapshotPlugin,
    SqliteStoragePlugin,
    SqliteTimeSeriesPlugin,
)
from .transport_ssh import SshTransportPlugin
from .ui_web import UiWebPlugin, WebPrompterPlugin

BUILTIN_PLUGINS: tuple[type[Plugin], ...] = (
    SqliteStoragePlugin,
    SqliteTimeSeriesPlugin,
    SqliteCampaignPlugin,
    SqliteSnapshotPlugin,
    SqliteRulePlugin,
    CampaignExcelPlugin,
    CliPrompterPlugin,
    SshTransportPlugin,
    SlurmSchedulerPlugin,
    MonitorPlugin,
    SnapshotPlugin,
    ConvergeSolverPlugin,
    OpenFoamSolverPlugin,
    SolverIngestPlugin,
    UiWebPlugin,
    WebPrompterPlugin,
    ExampleClockPlugin,
    ExampleTickerPlugin,
)

DISABLED_BY_DEFAULT: frozenset[str] = frozenset({"example-clock", "example-ticker"})

WEB_ONLY_PLUGINS: frozenset[str] = frozenset({"ui-web", "prompter-web"})
"""Started only by `simman serve`: other commands must not open a web server."""

PROMPTER_PLUGINS: dict[str, str] = {"cli": "prompter-cli", "web": "prompter-web"}
"""Providers of `auth.prompter`; exactly one runs (core config key `prompter`)."""
