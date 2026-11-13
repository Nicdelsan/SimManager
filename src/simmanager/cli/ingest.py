"""`simman ingest` subcommands for local solver ingestion and time-series inspection."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from simmanager import paths
from simmanager.app import AppSetup, prepare, start_kernel
from simmanager.contracts import ingest as ingest_contract
from simmanager.contracts import timeseries as ts_contract
from simmanager.contracts.ingest import IngestService
from simmanager.contracts.timeseries import TimeSeriesStore
from simmanager.kernel import ConfigError, Kernel
from simmanager.logging_setup import setup_logging
from simmanager.readers import LocalRemoteSession

ingest_app = typer.Typer(
    no_args_is_help=True,
    help="Ingest solver logs/outputs from a local directory and inspect stored time series.",
)

_console = Console()
_T = TypeVar("_T")

ProjectOpt = Annotated[
    Path | None,
    typer.Option("--project", "-p", help="Project directory or simmanager.yaml file."),
]


def _prepare(project: Path | None) -> AppSetup:
    try:
        setup = prepare(project)
    except ConfigError as exc:
        _console.print(f"[red]configuration error:[/] {exc}")
        raise typer.Exit(2) from exc
    setup_logging(paths.log_dir(), level=setup.core.log_level)
    for warning in setup.warnings:
        _console.print(f"[yellow]warning:[/] {warning}")
    return setup


def _with_kernel(setup: AppSetup, body: Callable[[Kernel], Awaitable[_T]]) -> _T:
    async def run() -> _T:
        kernel = await start_kernel(setup)
        try:
            return await body(kernel)
        finally:
            await kernel.stop()

    return asyncio.run(run())


def _fmt_float(value: float | None, precision: str = ".6g") -> str:
    return "-" if value is None else format(value, precision)


@ingest_app.command("local")
def ingest_local(
    work_dir: Annotated[Path, typer.Argument(help="Local simulation working directory.")],
    run_id: Annotated[int, typer.Option("--run-id", "-r", help="Run id to store under.")] = 1,
    solver: Annotated[
        str | None,
        typer.Option("--solver", "-s", help="Explicit solver kind (converge or openfoam)."),
    ] = None,
    stdout: Annotated[
        str | None,
        typer.Option("--stdout", help="Stdout log path relative to work_dir (e.g. log.out)."),
    ] = None,
    backfill: Annotated[
        bool,
        typer.Option("--backfill/--no-backfill", help="Finish full historical backfill inline."),
    ] = True,
    project: ProjectOpt = None,
) -> None:
    """Discover solver outputs in WORK_DIR, ingest them incrementally, and store time series."""
    if not work_dir.is_dir():
        _console.print(f"[red]directory not found:[/] {work_dir}")
        raise typer.Exit(2)

    setup = _prepare(project)
    session = LocalRemoteSession(work_dir)
    stdout_path = stdout
    if stdout_path is not None and not stdout_path.startswith("/"):
        stdout_path = f"{session.root_posix}/{stdout_path.lstrip('./')}"

    async def body(kernel: Kernel) -> None:
        ingestor: IngestService | None = kernel.services.get(ingest_contract.SERVICE_KEY)
        ts: TimeSeriesStore | None = kernel.services.get(ts_contract.SERVICE_KEY)
        if ingestor is None or ts is None:
            _console.print("[red]ingest/timeseries service unavailable[/] (see `simman plugins`)")
            raise typer.Exit(1)

        res = await ingestor.sync_run(
            session,
            run_id,
            session.root_posix,
            solver_kind=solver,
            stdout_path=stdout_path,
            run_backfill_immediately=backfill,
        )
        if res.solver is None:
            _console.print(f"[yellow]no supported solver detected in[/] {work_dir}")
            raise typer.Exit(1)

        state = await ts.get_run_solver_state(run_id)
        table = Table("field", "value", title=f"Ingested run {run_id}")
        table.add_row("solver", res.solver)
        table.add_row("state", str(res.solver_state))
        table.add_row("progress", f"{res.progress * 100:.1f}%" if res.progress is not None else "-")
        table.add_row("current_time", _fmt_float(state.current_time if state else None))
        table.add_row("ncyc", str(state.ncyc) if state and state.ncyc is not None else "-")
        table.add_row("warnings", str(state.warning_count if state else 0))
        table.add_row("files_synced", str(res.files_synced))
        table.add_row("bytes_read", str(res.bytes_read))
        table.add_row("points_ingested", str(res.points_ingested))
        table.add_row("backfill_pending", str(res.backfill_pending))
        table.add_row("watched_files", ", ".join(res.watched_files))
        _console.print(table)

    _with_kernel(setup, body)


@ingest_app.command("status")
def ingest_status(
    run_id: Annotated[int, typer.Argument(help="Run id to inspect.")],
    project: ProjectOpt = None,
) -> None:
    """Show solver state and per-file reading cursors for RUN_ID."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        ts: TimeSeriesStore | None = kernel.services.get(ts_contract.SERVICE_KEY)
        if ts is None:
            _console.print("[red]timeseries service unavailable[/]")
            raise typer.Exit(1)

        state = await ts.get_run_solver_state(run_id)
        cursors = await ts.list_cursors(run_id)
        if state is None and not cursors:
            _console.print(f"[yellow]no solver state or cursors found for run {run_id}[/]")
            raise typer.Exit(1)

        if state is not None:
            st = Table("field", "value", title=f"Run {run_id} solver state")
            st.add_row("solver", state.solver)
            st.add_row("state", str(state.solver_state))
            st.add_row(
                "progress", f"{state.progress * 100:.1f}%" if state.progress is not None else "-"
            )
            st.add_row(
                "time",
                f"{_fmt_float(state.start_time)} -> {_fmt_float(state.current_time)} "
                f"(end {_fmt_float(state.end_time)})",
            )
            st.add_row("ncyc", str(state.ncyc) if state.ncyc is not None else "-")
            st.add_row("warnings", str(state.warning_count))
            st.add_row("output_dir", state.output_dir or "-")
            if state.error_message:
                st.add_row("error", state.error_message)
            _console.print(st)

        ct = Table("file", "parser", "status", "size", "forward", "backfill_gap", title="Cursors")
        for c in cursors:
            gap = f"{c.backfill_start}..{c.backfill_end}" if c.needs_backfill else "done"
            ct.add_row(
                c.file_path,
                c.parser_kind,
                str(c.status),
                str(c.file_size),
                str(c.forward_offset),
                gap,
            )
        _console.print(ct)

    _with_kernel(setup, body)


@ingest_app.command("series")
def ingest_series(
    run_id: Annotated[int, typer.Argument(help="Run id to query.")],
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="Filter series whose name contains this text."),
    ] = None,
    file: Annotated[
        str | None,
        typer.Option("--file", "-f", help="Filter series whose file_path contains this text."),
    ] = None,
    points: Annotated[
        bool,
        typer.Option("--points", help="Query and print sample (x, y) points for matched series."),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="Maximum series/points rows.")] = 30,
    max_points: Annotated[
        int,
        typer.Option("--max-points", help="Downsampled point budget when querying with --points."),
    ] = 200,
    project: ProjectOpt = None,
) -> None:
    """List time series for RUN_ID, or print downsampled points with --points."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        ts: TimeSeriesStore | None = kernel.services.get(ts_contract.SERVICE_KEY)
        if ts is None:
            _console.print("[red]timeseries service unavailable[/]")
            raise typer.Exit(1)

        all_series = await ts.list_series(run_id)
        matched = [
            s
            for s in all_series
            if (name is None or name.lower() in s.name.lower())
            and (file is None or file.lower() in s.file_path.lower())
        ]
        if not matched:
            _console.print(f"[yellow]no matching series for run {run_id}[/]")
            raise typer.Exit(1)

        table = Table(
            "id",
            "file",
            "name",
            "qualifier",
            "unit",
            "points",
            "last_x",
            "last_y",
            "min_y",
            "max_y",
            title=f"Series for run {run_id} ({len(matched)} total)",
        )
        for s in matched[:limit]:
            table.add_row(
                str(s.id),
                s.file_path,
                s.name,
                s.qualifier,
                s.unit,
                str(s.point_count),
                _fmt_float(s.last_x),
                _fmt_float(s.last_y),
                _fmt_float(s.min_y),
                _fmt_float(s.max_y),
            )
        _console.print(table)

        if points:
            target = matched[0]
            res = await ts.query_series(target.id, max_points=max_points)
            title = (
                f"Series #{target.id} ({target.file_path} :: {target.name}, "
                f"level={res.level}, total={res.total_points})"
            )
            pt = Table(target.x_name or "x", target.name, title=title)
            for x_val, y_val in list(zip(res.x, res.y, strict=True))[:limit]:
                pt.add_row(_fmt_float(x_val), _fmt_float(y_val))
            _console.print(pt)

    _with_kernel(setup, body)
