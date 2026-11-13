"""`simman` command line."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
import yaml
from rich.console import Console
from rich.table import Table

from simmanager import __version__, paths
from simmanager.app import AppSetup, prepare, start_kernel
from simmanager.contracts.storage import Storage
from simmanager.kernel import ConfigError, Kernel, PluginState
from simmanager.logging_setup import setup_logging

from .ingest import ingest_app

app = typer.Typer(no_args_is_help=True, help="SimManager: monitor CFD simulations on HPC clusters.")
config_app = typer.Typer(no_args_is_help=True, help="Inspect the layered configuration.")
app.add_typer(config_app, name="config")
app.add_typer(ingest_app, name="ingest")

console = Console()
T = TypeVar("T")

ProjectOpt = Annotated[
    Path | None,
    typer.Option("--project", "-p", help="Project directory or simmanager.yaml file."),
]

_STATE_STYLE = {
    PluginState.ACTIVE: "green",
    PluginState.PENDING: "yellow",
    PluginState.FAILED: "red",
}


def _prepare(project: Path | None) -> AppSetup:
    try:
        setup = prepare(project)
    except ConfigError as exc:
        console.print(f"[red]configuration error:[/] {exc}")
        raise typer.Exit(2) from exc
    setup_logging(paths.log_dir(), level=setup.core.log_level)
    for warning in setup.warnings:
        console.print(f"[yellow]warning:[/] {warning}")
    return setup


def _with_kernel(setup: AppSetup, body: Callable[[Kernel], Awaitable[T]]) -> T:
    async def run() -> T:
        kernel = await start_kernel(setup)
        try:
            return await body(kernel)
        finally:
            await kernel.stop()

    return asyncio.run(run())


@app.command()
def version() -> None:
    """Print the SimManager version."""
    console.print(__version__)


@app.command()
def plugins(project: ProjectOpt = None) -> None:
    """Start the kernel once and show the state of every plugin."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        await asyncio.sleep(0)  # let freshly started tasks run once
        table = Table("plugin", "state", "provides", "needs", "notes")
        for info in sorted(kernel.info(), key=lambda i: i.name):
            style = _STATE_STYLE.get(info.state, "")
            needs = ", ".join(f"{k} v{v}" for k, v in info.inject.items())
            notes = info.error or (
                f"waiting for: {', '.join(info.waiting_for)}"
                if info.waiting_for
                else info.description
            )
            table.add_row(info.name, f"[{style}]{info.state}[/]", info.provides or "", needs, notes)
        for name in setup.disabled:
            table.add_row(
                name, "[dim]disabled[/]", "", "", "enable with plugins.<name>.enabled: true"
            )
        console.print(table)

    _with_kernel(setup, body)


@app.command()
def serve(project: ProjectOpt = None) -> None:
    """Run SimManager until Ctrl+C (the web UI is added in M3)."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        active = [i.name for i in kernel.info() if i.state is PluginState.ACTIVE]
        console.print(f"SimManager running with: {', '.join(active)}. Ctrl+C to stop.")
        await asyncio.Event().wait()

    with contextlib.suppress(KeyboardInterrupt):
        _with_kernel(setup, body)
    console.print("stopped.")


@app.command()
def events(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of events.")] = 20,
    project: ProjectOpt = None,
) -> None:
    """Show the most recent entries of the durable event log."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        storage: Storage | None = kernel.services.get("storage")
        if storage is None:
            console.print("[red]storage service not available[/] (see `simman plugins`)")
            raise typer.Exit(1)
        table = Table("id", "time (UTC)", "kind", "source", "cluster", "run", "payload")
        for e in reversed(await storage.latest_events(limit)):
            table.add_row(
                str(e.id),
                e.ts.strftime("%Y-%m-%d %H:%M:%S"),
                e.kind,
                e.source,
                e.cluster_id or "",
                "" if e.run_id is None else str(e.run_id),
                str(e.payload),
            )
        console.print(table)

    _with_kernel(setup, body)


@config_app.command("show")
def config_show(
    origin: Annotated[
        bool, typer.Option("--origin", help="Show which layer set each value.")
    ] = False,
    project: ProjectOpt = None,
) -> None:
    """Print the composed configuration (defaults -> user -> project)."""
    setup = _prepare(project)
    for layer in setup.config.layers:
        where = str(layer.path) if layer.path else "built-in"
        exists = "" if layer.path is None or layer.path.exists() else " (missing)"
        console.print(f"[dim]# layer {layer.name}: {where}{exists}[/]")
    if not origin:
        console.print(yaml.safe_dump(setup.config.data, sort_keys=False, allow_unicode=True))
        return
    table = Table("key", "value", "from")
    for key_path, layer_name in sorted(setup.config.origins.items()):
        table.add_row(".".join(key_path), repr(_lookup(setup.config.data, key_path)), layer_name)
    console.print(table)


@config_app.command("paths")
def config_paths() -> None:
    """Show where SimManager reads configuration and writes data."""
    console.print(f"user config : {paths.user_config_file()}")
    console.print(f"clusters    : {paths.clusters_file()}")
    console.print(f"data        : {paths.data_dir()}")
    console.print(f"database    : {paths.default_database_file()}")
    console.print(f"logs        : {paths.log_dir()}")


def _lookup(data: Any, key_path: tuple[str, ...]) -> Any:
    for key in key_path:
        data = data[key]
    return data


if __name__ == "__main__":
    app()
