"""`simman` command line."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from simmanager import __version__, paths
from simmanager.app import AppSetup, prepare, start_kernel
from simmanager.contracts.clusters import ClusterProfile
from simmanager.contracts.monitor import Monitor
from simmanager.contracts.storage import Storage
from simmanager.contracts.transport import (
    ClusterUnavailableError,
    CommandResult,
    RemoteSession,
    Transport,
    TransportError,
)
from simmanager.kernel import ConfigError, Kernel, PluginState
from simmanager.kernel.config import format_validation_error
from simmanager.logging_setup import setup_logging
from simmanager.plugins.scheduler_slurm.parse import SACCT_DISABLED, parse_squeue, squeue_argv
from simmanager.plugins.transport_ssh import load_clusters

from .campaign import campaign_app
from .ingest import ingest_app

app = typer.Typer(no_args_is_help=True, help="SimManager: monitor CFD simulations on HPC clusters.")
config_app = typer.Typer(no_args_is_help=True, help="Inspect the layered configuration.")
app.add_typer(config_app, name="config")
app.add_typer(ingest_app, name="ingest")
app.add_typer(campaign_app, name="campaign")
cluster_app = typer.Typer(no_args_is_help=True, help="Cluster profiles (clusters.yaml).")
app.add_typer(cluster_app, name="cluster")
jobs_app = typer.Typer(no_args_is_help=True, help="Jobs discovered by the monitor.")
app.add_typer(jobs_app, name="jobs")

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


def _prepare(project: Path | None, *, monitor_loop: bool = False) -> AppSetup:
    """Compose config and logging. The monitor polls in the background only when asked
    (`serve`, `monitor`): other commands must not open SSH connections by themselves."""
    try:
        setup = prepare(project)
    except ConfigError as exc:
        console.print(f"[red]configuration error:[/] {exc}")
        raise typer.Exit(2) from exc
    setup.core.plugins.setdefault("monitor", {})["loop"] = monitor_loop
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
    """Run SimManager until Ctrl+C: monitors the clusters (the web UI is added in M3)."""
    setup = _prepare(project, monitor_loop=True)

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


# --------------------------------------------------------------------- clusters
def _load_profiles() -> dict[str, ClusterProfile]:
    try:
        return load_clusters(paths.clusters_file())
    except ConfigError as exc:
        console.print(f"[red]clusters.yaml error:[/] {exc}")
        raise typer.Exit(2) from exc


@cluster_app.command("list")
def cluster_list() -> None:
    """Show the configured clusters."""
    profiles = _load_profiles()
    if not profiles:
        console.print(f"no clusters in {paths.clusters_file()}: add one with `simman cluster add`")
        return
    table = Table("id", "target", "ssh config host", "scheduler", "timezone", "keys")
    for cid, p in profiles.items():
        table.add_row(
            cid,
            p.target(),
            p.ssh_config_host or "",
            p.scheduler,
            p.timezone or "(local)",
            str(len(p.key_files)) if p.key_files else "(ssh config / agent)",
        )
    console.print(table)


@cluster_app.command("add")
def cluster_add(
    cluster_id: Annotated[str, typer.Argument(help="Short id, e.g. uni-a.")],
    ssh_config_host: Annotated[
        str | None, typer.Option("--ssh-host", help="Host alias in ~/.ssh/config.")
    ] = None,
    host: Annotated[str | None, typer.Option(help="Host name (if not in the SSH config).")] = None,
    port: Annotated[int | None, typer.Option(help="SSH port.")] = None,
    user: Annotated[str | None, typer.Option(help="Remote user name.")] = None,
    key: Annotated[
        list[str] | None, typer.Option("--key", help="Private key file (repeatable).")
    ] = None,
    ssh_config_file: Annotated[
        str | None, typer.Option("--ssh-config", help="OpenSSH config file to read.")
    ] = None,
    known_hosts: Annotated[str | None, typer.Option(help="known_hosts file.")] = None,
    timezone: Annotated[
        str | None, typer.Option(help="IANA zone of cluster timestamps, e.g. Europe/Rome.")
    ] = None,
    renew_command: Annotated[
        str | None,
        typer.Option(help="Local command that renews an expired SSH certificate."),
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing profile.")] = False,
) -> None:
    """Add a cluster profile to clusters.yaml (outside the repository)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", cluster_id):
        console.print("[red]cluster id:[/] letters, digits, '-', '_' and '.' only")
        raise typer.Exit(2)
    path = paths.clusters_file()
    data: dict[str, Any] = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    clusters = data.setdefault("clusters", {})
    if cluster_id in clusters and not force:
        console.print(f"[red]{cluster_id} already exists[/] (use --force to replace it)")
        raise typer.Exit(1)
    raw: dict[str, Any] = {
        "ssh_config_host": ssh_config_host,
        "host": host,
        "port": port,
        "user": user,
        "key_files": key or [],
        "ssh_config_file": ssh_config_file,
        "known_hosts": known_hosts,
        "timezone": timezone,
        "cert_renew_command": split_command(renew_command) if renew_command else None,
    }
    try:
        profile = ClusterProfile.model_validate({k: v for k, v in raw.items() if v})
    except ValidationError as exc:
        console.print(f"[red]invalid profile:[/]\n{format_validation_error(exc)}")
        raise typer.Exit(2) from exc
    if not (profile.host or profile.ssh_config_host):
        console.print("[red]give --ssh-host or --host[/]")
        raise typer.Exit(2)
    clusters[cluster_id] = profile.model_dump(mode="json", exclude_defaults=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    console.print(f"saved {cluster_id} in {path}. Try it: simman cluster test {cluster_id}")


_QUOTES = ("'", '"')


def split_command(command: str) -> list[str]:
    """Split like a shell but keep Windows backslashes; strip surrounding quotes."""
    parts = shlex.split(command, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in _QUOTES else p for p in parts]


@cluster_app.command("test")
def cluster_test(
    cluster_id: Annotated[str, typer.Argument(help="Cluster id from clusters.yaml.")],
    project: ProjectOpt = None,
) -> None:
    """Connect, check the SLURM commands SimManager needs, report what was found."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        transport: Transport | None = kernel.services.get("transport")
        if transport is None:
            console.print("[red]transport not available[/] (see `simman plugins`)")
            raise typer.Exit(1)
        if cluster_id not in transport.clusters():
            console.print(f"[red]unknown cluster {cluster_id}[/]; see `simman cluster list`")
            raise typer.Exit(1)
        await transport.reset(cluster_id)
        try:
            session = await transport.session(cluster_id)
        except ClusterUnavailableError as exc:
            console.print(f"[red]cannot connect:[/] {exc.state}: {exc.detail}")
            raise typer.Exit(1) from exc
        console.print(f"[green]connected[/] to {cluster_id} as {session.user}")
        table = Table("check", "result", "detail")
        for name, ok, detail in await run_cluster_checks(session):
            mark = "[green]ok[/]" if ok else "[yellow]limited[/]" if ok is None else "[red]no[/]"
            table.add_row(name, mark, detail)
        console.print(table)

    _with_kernel(setup, body)


async def run_cluster_checks(session: RemoteSession) -> list[tuple[str, bool | None, str]]:
    """(check, ok, detail) rows for `cluster test`; ok None = works with limitations."""
    rows: list[tuple[str, bool | None, str]] = []

    async def run(argv: list[str]) -> CommandResult | None:
        try:
            return await session.run_readonly(argv, timeout_s=60)
        except TransportError as exc:
            rows.append((" ".join(argv[:2]), False, str(exc)))
            return None

    if (r := await run(["hostname"])) is not None:
        rows.append(("hostname", r.ok, r.stdout.strip()))
    if (r := await run(["sinfo", "--version"])) is not None:
        rows.append(("slurm version", r.ok, (r.stdout or r.stderr).strip()))
    if (r := await run(squeue_argv(session.user))) is not None:
        detail = f"{len(parse_squeue(r.stdout, None))} job(s) of {session.user}"
        rows.append(("squeue format (A2)", r.ok, detail if r.ok else r.stderr.strip()[:200]))
    if (r := await run(["sacct", "-X", "-n", "-P", "-o", "JobID", "-j", "1"])) is not None:
        if SACCT_DISABLED in (r.stdout + r.stderr).lower():
            rows.append(("accounting (sacct)", None, "disabled: final states via scontrol"))
        else:
            rows.append(("accounting (sacct)", r.ok, "enabled" if r.ok else r.stderr.strip()))
    if (r := await run(["scontrol", "show", "config"])) is not None:
        ages = [ln.split("=", 1)[1].strip() for ln in r.stdout.splitlines() if "MinJobAge" in ln]
        rows.append(("MinJobAge (A3)", r.ok, ages[0] if ages else "not found"))
    return rows


# ------------------------------------------------------------------ monitoring
@app.command()
def monitor(
    once: Annotated[bool, typer.Option("--once", help="Poll every cluster once and exit.")] = False,
    project: ProjectOpt = None,
) -> None:
    """Poll the clusters' schedulers and record jobs (Ctrl+C to stop)."""
    setup = _prepare(project, monitor_loop=not once)

    async def body(kernel: Kernel) -> None:
        mon: Monitor | None = kernel.services.get("monitor")
        if mon is None:
            console.print("[red]monitor not available[/] (see `simman plugins`)")
            raise typer.Exit(1)
        if not once:
            console.print("monitoring... Ctrl+C to stop. See `simman jobs list`.")
            await asyncio.Event().wait()
        table = Table("cluster", "ok", "active jobs", "new", "changed", "next poll", "error")
        for r in await mon.poll_all():
            table.add_row(
                r.cluster_id,
                "[green]yes[/]" if r.ok else "[red]no[/]",
                str(r.active_jobs),
                str(r.discovered),
                str(r.changed),
                f"{r.next_poll_s:.0f} s",
                r.error or "",
            )
        console.print(table)

    with contextlib.suppress(KeyboardInterrupt):
        _with_kernel(setup, body)


@jobs_app.command("list")
def jobs_list(
    cluster: Annotated[
        str | None, typer.Option("--cluster", "-c", help="Only this cluster.")
    ] = None,
    show_all: Annotated[bool, typer.Option("--all", "-a", help="Include ended jobs.")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Maximum rows.")] = 50,
    project: ProjectOpt = None,
) -> None:
    """Show jobs recorded by the monitor (active ones by default)."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        storage: Storage | None = kernel.services.get("storage")
        if storage is None:
            console.print("[red]storage service not available[/]")
            raise typer.Exit(1)
        records = await storage.jobs(
            cluster_id=cluster, active=None if show_all else True, limit=limit
        )
        table = Table("cluster", "job", "name", "state", "elapsed", "submitted", "work dir")
        for rec in records:
            i = rec.info
            submitted = (
                i.submit_time.astimezone().strftime("%Y-%m-%d %H:%M") if i.submit_time else ""
            )
            reason = f" ({i.reason})" if i.reason and not i.state.terminal else ""
            table.add_row(
                rec.cluster_id,
                i.job_id,
                i.name,
                f"{i.state}{reason}",
                fmt_duration(i.elapsed_s),
                submitted,
                i.work_dir or "",
            )
        console.print(table)
        if not records:
            console.print("no jobs recorded yet: run `simman monitor --once`")

    _with_kernel(setup, body)


def fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    days, rest = divmod(seconds, 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    return (f"{days}-" if days else "") + f"{h:02d}:{m:02d}:{s:02d}"


if __name__ == "__main__":
    app()
