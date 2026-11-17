"""`simman snapshot` and `simman run` subcommands."""

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
from simmanager.contracts import snapshot as snapshot_contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts import transport as transport_contract
from simmanager.contracts.snapshot import DiffChangeKind, SnapshotService, SnapshotTrigger
from simmanager.contracts.storage import Storage
from simmanager.contracts.transport import RemoteSession, Transport, TransportError
from simmanager.kernel import ConfigError, Kernel
from simmanager.logging_setup import setup_logging
from simmanager.readers import LocalRemoteSession

snapshot_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect input snapshots, compare runs, or capture a manual snapshot.",
)
run_app = typer.Typer(
    no_args_is_help=True,
    help="Manage run metadata and free-text notes.",
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
    setup.core.plugins.setdefault("monitor", {})["loop"] = False
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


def _require_snapshot(kernel: Kernel) -> tuple[Storage, SnapshotService]:
    storage: Storage | None = kernel.services.get(storage_contract.SERVICE_KEY)
    mgr: SnapshotService | None = kernel.services.get(snapshot_contract.MANAGER_SERVICE_KEY)
    if storage is None or mgr is None:
        _console.print("[red]snapshot service unavailable[/] (see `simman plugins`)")
        raise typer.Exit(1)
    return storage, mgr


@snapshot_app.command("list")
def snapshot_list(
    run_id: Annotated[int, typer.Argument(help="Run id.")],
    project: ProjectOpt = None,
) -> None:
    """List captured input snapshots for RUN_ID."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        _, mgr = _require_snapshot(kernel)
        snaps = await mgr.list_snapshots(run_id)
        if not snaps:
            _console.print(f"no snapshots recorded for run {run_id}")
            return
        table = Table(
            "id",
            "captured (UTC)",
            "trigger",
            "eff",
            "late",
            "status",
            "solver",
            "files",
            "bytes",
            title=f"Snapshots for run {run_id}",
        )
        for s in snaps:
            table.add_row(
                str(s.id),
                s.captured_at.strftime("%Y-%m-%d %H:%M:%S"),
                str(s.trigger),
                "yes" if s.effective else "",
                "[yellow]LATE[/]" if s.late else "",
                str(s.status),
                s.solver or "",
                str(s.file_count),
                str(s.total_bytes),
            )
        _console.print(table)

    _with_kernel(setup, body)


@snapshot_app.command("show")
def snapshot_show(
    snapshot_id: Annotated[int, typer.Argument(help="Snapshot id.")],
    file: Annotated[
        str | None,
        typer.Option("--file", "-f", help="Print the stored text content of this relative path."),
    ] = None,
    project: ProjectOpt = None,
) -> None:
    """Show manifest details of SNAPSHOT_ID (or print a stored file with --file)."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        _, mgr = _require_snapshot(kernel)
        snap = await mgr.get_snapshot(snapshot_id, include_files=True)
        if snap is None:
            _console.print(f"[red]snapshot {snapshot_id} not found[/]")
            raise typer.Exit(1)

        if file is not None:
            text = await mgr.get_file_text(snapshot_id, file)
            if text is None:
                _console.print(
                    f"[red]file {file!r} not stored as text in snapshot {snapshot_id}[/]"
                )
                raise typer.Exit(1)
            _console.print(text, markup=False)
            return

        if snap.late:
            _console.print(
                f"[yellow]WARNING:[/] possibly modified after launch ({', '.join(snap.late_files)})"
            )
        meta = Table("field", "value", title=f"Snapshot #{snap.id} (run {snap.run_id})")
        meta.add_row("trigger", str(snap.trigger))
        meta.add_row("effective", str(snap.effective))
        meta.add_row("status", str(snap.status))
        meta.add_row("late", f"yes ({', '.join(snap.late_files)})" if snap.late else "no")
        meta.add_row("solver", snap.solver or "-")
        meta.add_row("work_dir", snap.work_dir)
        meta.add_row("manifest_sha256", snap.manifest_sha256[:16])
        meta.add_row("captured_at", snap.captured_at.isoformat())
        _console.print(meta)

        ft = Table("path", "size", "status", "sha256", title="Files")
        for f in snap.files:
            ft.add_row(
                f.rel_path,
                str(f.size_bytes),
                str(f.content_status),
                (f.sha256[:12] if f.sha256 else "-"),
            )
        _console.print(ft)

    _with_kernel(setup, body)


@snapshot_app.command("diff")
def snapshot_diff(
    run_id: Annotated[int, typer.Argument(help="Target run id.")],
    against: Annotated[
        int | None,
        typer.Option("--against", "-a", help="Explicit base run id to compare against."),
    ] = None,
    parent: Annotated[
        bool,
        typer.Option("--parent", help="Compare against the derived case's parent run."),
    ] = False,
    unified: Annotated[
        bool,
        typer.Option("--unified", "-u", help="Include unified text diffs."),
    ] = False,
    project: ProjectOpt = None,
) -> None:
    """Compare the effective snapshot of RUN_ID against its base (previous run or --parent)."""
    if against is not None and parent:
        _console.print("[red]use either --against or --parent, not both[/]")
        raise typer.Exit(2)
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        _, mgr = _require_snapshot(kernel)
        try:
            diff = await mgr.diff_runs(run_id, against_run_id=against, parent=parent)
        except LookupError as exc:
            _console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc

        base_desc = (
            f"run {diff.base_snapshot.run_id} (snap #{diff.base_snapshot.id})"
            if diff.base_snapshot is not None
            else "(none)"
        )
        _console.print(
            f"Diff run {run_id} (snap #{diff.target_snapshot.id}) vs {base_desc} "
            f"[{diff.base_reason}]"
        )
        if diff.target_late:
            _console.print(
                "[yellow]WARNING: target snapshot possibly modified after launch:[/] "
                + ", ".join(diff.target_late_files)
            )
        if diff.base_late:
            _console.print("[yellow]WARNING: base snapshot was marked late[/]")

        if not diff.has_changes and not diff.files:
            _console.print("[green]no input differences[/]")
            return

        sem_rows: list[tuple[str, str, str, str]] = []
        file_table = Table("path", "change", "base size", "target size", "notes")
        for f in diff.files:
            if f.change_kind is DiffChangeKind.OVERSIZE_UNCHANGED:
                continue
            file_table.add_row(
                f.rel_path,
                str(f.change_kind),
                "" if f.base_size is None else str(f.base_size),
                "" if f.target_size is None else str(f.target_size),
                f.note or "",
            )
            for sc in f.semantic_changes:
                key = f"[{sc.doc_index}] {sc.path}" if sc.doc_index is not None else sc.path
                sem_rows.append(
                    (
                        f.rel_path,
                        key,
                        "-" if sc.old_value is None else sc.old_value,
                        "-" if sc.new_value is None else sc.new_value,
                    )
                )

        if file_table.row_count == 0:
            _console.print("[green]no input differences[/]")
            return
        _console.print(file_table)

        if sem_rows:
            st = Table("file", "key", "old", "new", title="Semantic changes")
            for row in sem_rows:
                st.add_row(*row)
            _console.print(st)

        if unified:
            for f in diff.files:
                if f.unified_diff:
                    _console.print(f.unified_diff, markup=False)

    _with_kernel(setup, body)


@snapshot_app.command("take")
def snapshot_take(
    run_id: Annotated[int, typer.Argument(help="Run id to snapshot manually.")],
    local_dir: Annotated[
        Path | None,
        typer.Option("--local", help="Read inputs from a local directory instead of SSH."),
    ] = None,
    solver: Annotated[
        str | None,
        typer.Option("--solver", "-s", help="Explicit solver kind (converge or openfoam)."),
    ] = None,
    project: ProjectOpt = None,
) -> None:
    """Capture a manual input snapshot for RUN_ID."""
    if local_dir is not None and not local_dir.is_dir():
        _console.print(f"[red]directory not found:[/] {local_dir}")
        raise typer.Exit(2)
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        storage, mgr = _require_snapshot(kernel)
        run = await storage.run(run_id)
        if run is None:
            _console.print(f"[red]run {run_id} not found[/]")
            raise typer.Exit(1)

        session: RemoteSession
        work_dir = run.work_dir
        if local_dir is not None:
            local_sess = LocalRemoteSession(local_dir, virtual_root=work_dir)
            session = local_sess
            work_dir = work_dir or local_sess.root_posix
        else:
            if not run.cluster_id or not work_dir:
                _console.print("[red]run has no cluster_id/work_dir; pass --local DIR[/]")
                raise typer.Exit(1)
            transport: Transport | None = kernel.services.get(transport_contract.SERVICE_KEY)
            if transport is None:
                _console.print("[red]transport service unavailable[/]")
                raise typer.Exit(1)
            try:
                session = await transport.session(run.cluster_id)
            except TransportError as exc:
                _console.print(f"[red]cannot connect to {run.cluster_id}:[/] {exc}")
                raise typer.Exit(1) from exc

        res = await mgr.capture_run(
            session,
            run_id,
            work_dir,
            trigger=SnapshotTrigger.MANUAL,
            solver_kind=solver,
            effective=True,
            wait_for_hash=True,
        )
        s = res.snapshot
        _console.print(
            f"Captured snapshot #{s.id} for run {run_id}: {s.file_count} files, "
            f"{s.total_bytes} bytes, status={s.status}"
            + (" [yellow](LATE: possibly modified after launch)[/]" if s.late else "")
        )

    _with_kernel(setup, body)


@run_app.command("note")
def run_note(
    run_id: Annotated[int, typer.Argument(help="Run id.")],
    text: Annotated[
        str | None,
        typer.Argument(help="Note text to set (omit to show current note)."),
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Clear the run note.")] = False,
    project: ProjectOpt = None,
) -> None:
    """View or update the free-text note attached to RUN_ID."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        storage, mgr = _require_snapshot(kernel)
        if await storage.run(run_id) is None:
            _console.print(f"[red]run {run_id} not found[/]")
            raise typer.Exit(1)

        if clear:
            await mgr.set_note(run_id, "")
            _console.print(f"Cleared note for run {run_id}.")
            return
        if text is not None:
            rec = await mgr.set_note(run_id, text)
            _console.print(f"Saved note for run {run_id} ({len(rec.note)} chars).")
            return

        existing = await mgr.get_note(run_id)
        if existing is None or not existing.note:
            _console.print(f"(no note for run {run_id})")
        else:
            _console.print(existing.note, markup=False)

    _with_kernel(setup, body)
