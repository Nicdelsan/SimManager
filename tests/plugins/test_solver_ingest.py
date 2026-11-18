from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from simmanager.contracts import ingest as ingest_contract
from simmanager.contracts import monitor as monitor_contract
from simmanager.contracts import scheduler as scheduler_contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts import timeseries as ts_contract
from simmanager.contracts import transport as transport_contract
from simmanager.contracts.clusters import ClusterProfile
from simmanager.contracts.ingest import IngestService
from simmanager.contracts.monitor import Monitor, PollReport
from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.contracts.storage import Storage
from simmanager.contracts.timeseries import SolverState, TimeSeriesStore
from simmanager.contracts.transport import (
    ClusterUnavailableError,
    ConnectionState,
    RemoteSession,
)
from simmanager.kernel import Context, Kernel, Plugin, PluginState
from simmanager.plugins.monitor import MonitorPlugin
from simmanager.plugins.solver_converge import ConvergeSolverPlugin
from simmanager.plugins.solver_ingest import SolverIngestPlugin
from simmanager.plugins.solver_openfoam import OpenFoamSolverPlugin
from simmanager.plugins.storage_sqlite import SqliteStoragePlugin, SqliteTimeSeriesPlugin
from simmanager.readers import LocalRemoteSession

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures"


class _FakeTransport:
    def __init__(
        self,
        sessions: dict[str, RemoteSession],
        clusters: dict[str, ClusterProfile] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clusters = clusters or {}
        self.fail_next: ClusterUnavailableError | None = None

    def clusters(self) -> Mapping[str, ClusterProfile]:
        return self._clusters

    async def session(self, cluster_id: str) -> RemoteSession:
        if self.fail_next is not None:
            err = self.fail_next
            self.fail_next = None
            raise err
        return self._sessions[cluster_id]

    def state(self, cluster_id: str) -> ConnectionState:
        return (
            ConnectionState.CONNECTED
            if cluster_id in self._sessions
            else ConnectionState.DISCONNECTED
        )

    async def reset(self, cluster_id: str) -> None:
        return None


async def _wait_until(predicate: Any, *, timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for predicate")


async def test_sync_run_converge_and_openfoam_fixtures(tmp_path: Path) -> None:
    db_path = str(tmp_path / "sim.db")
    kernel = Kernel({"storage-sqlite": {"path": db_path}})
    await kernel.add(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        ConvergeSolverPlugin,
        OpenFoamSolverPlugin,
        SolverIngestPlugin,
    )
    assert kernel.state("solver-ingest") is PluginState.ACTIVE

    ingestor: IngestService = kernel.services.require(ingest_contract.SERVICE_KEY)
    ts: TimeSeriesStore = kernel.services.require(ts_contract.SERVICE_KEY)
    storage: Storage = kernel.services.require(storage_contract.SERVICE_KEY)
    bus_kinds: list[str] = []
    for kind in ("series.updated", "run.progress"):

        async def record(payload: object, kind: str = kind) -> None:
            bus_kinds.append(kind)

        kernel.bus.on(kind, record)

    conv_session = LocalRemoteSession(FIXTURES_ROOT / "converge")
    r_conv = await storage.create_run(work_dir=conv_session.root_posix)
    res_conv = await ingestor.sync_run(
        conv_session, run_id=r_conv.id, work_dir=conv_session.root_posix
    )
    assert res_conv.solver == "converge"
    assert res_conv.files_synced >= 3
    assert res_conv.points_ingested > 0
    assert res_conv.solver_state is SolverState.COMPLETED
    assert res_conv.progress == pytest.approx(1.0)

    of_session = LocalRemoteSession(FIXTURES_ROOT / "openfoam")
    r_of = await storage.create_run(work_dir=of_session.root_posix)
    res_of = await ingestor.sync_run(of_session, run_id=r_of.id, work_dir=of_session.root_posix)
    assert res_of.solver == "openfoam"
    assert res_of.points_ingested > 0
    assert res_of.solver_state is SolverState.COMPLETED
    assert res_of.progress == pytest.approx(1.0)

    # Durable log: facts only. Data/progress notifications: bus only.
    ev_kinds = {e.kind for e in await storage.events(run_id=r_conv.id)}
    assert {"run.solver_state_changed", "solver.finished"} <= ev_kinds
    assert ev_kinds <= ingest_contract.DURABLE_EVENT_KINDS
    assert {"series.updated", "run.progress"} <= set(bus_kinds)

    # Progress against wall time, for the ETA and the progress plot.
    progress = [
        s
        for s in await ts.list_series(r_conv.id)
        if s.file_path == ingest_contract.PROGRESS_SERIES.file_path
    ]
    assert len(progress) == 1 and progress[0].last_y == pytest.approx(1.0)
    assert progress[0].last_x is not None and progress[0].last_x > 1.7e9  # epoch seconds

    series_conv = await ts.list_series(r_conv.id)
    assert any(s.file_path == "thermo.out" for s in series_conv)
    assert (await ts.get_run_solver_state(r_of.id)) is not None
    await kernel.stop()


async def test_auto_backfill_background_task(tmp_path: Path) -> None:
    case_dir = tmp_path / "big_converge"
    out_dir = case_dir / "outputs_0" / "stream0"
    out_dir.mkdir(parents=True)
    (case_dir / "inputs.in").write_text("---\nend_time: 1.0\n", encoding="utf-8")

    # > 1 MiB synthetic thermo.out so full_read_max_mb=1 triggers tail preview + background backfill
    lines = ["# column_names\n", "# Time Mean_Temp\n"]
    for i in range(45_000):
        lines.append(f"  {i * 1e-5:.6e}   {300.0 + (i % 100):.6f}\n")
    (out_dir / "thermo.out").write_text("".join(lines), encoding="utf-8")

    db_path = str(tmp_path / "sim.db")
    kernel = Kernel(
        {
            "storage-sqlite": {"path": db_path},
            "solver-ingest": {
                "chunk_mb": 1,
                "full_read_max_mb": 1,
                "tail_preview_mb": 1,
                "auto_backfill": True,
                "poll_interval_s": 60.0,
            },
        }
    )
    await kernel.add(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        ConvergeSolverPlugin,
        SolverIngestPlugin,
    )
    ingestor: IngestService = kernel.services.require(ingest_contract.SERVICE_KEY)
    storage: Storage = kernel.services.require(storage_contract.SERVICE_KEY)
    ts: TimeSeriesStore = kernel.services.require(ts_contract.SERVICE_KEY)

    backfill_events: list[dict[str, Any]] = []
    kernel.bus.on("backfill.progress", lambda p: backfill_events.append(dict(p)))

    session = LocalRemoteSession(case_dir, virtual_root="/case")
    run_id = (await storage.create_run(work_dir="/case")).id
    res = await ingestor.sync_run(session, run_id=run_id, work_dir="/case")
    assert res.backfill_pending == 1

    async def _is_backfill_done() -> bool:
        cursors = await ts.list_cursors(run_id)
        return bool(cursors) and not any(c.needs_backfill for c in cursors)

    await _wait_until(_is_backfill_done)
    assert any(e["done"] is True for e in backfill_events)

    series = await ts.list_series(run_id)
    temp = next(s for s in series if s.name == "Mean_Temp")
    assert temp.point_count == 45_000
    await kernel.stop()


async def test_monitor_polled_resilience_bootstrap_and_durable_state(tmp_path: Path) -> None:
    conv_session = LocalRemoteSession(
        FIXTURES_ROOT / "converge", cluster_id="c1", virtual_root="/case"
    )
    fake_transport = _FakeTransport({"c1": conv_session})

    class FakeTransportPlugin(Plugin):
        name = "fake-transport"
        provides = transport_contract.SERVICE_KEY
        contract_version = transport_contract.CONTRACT_VERSION

        def apply(self, ctx: Context) -> _FakeTransport:
            return fake_transport

    db_path = str(tmp_path / "sim.db")
    kernel = Kernel(
        {
            "storage-sqlite": {"path": db_path},
            "solver-ingest": {"poll_interval_s": 0.05},
        }
    )
    await kernel.add(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        ConvergeSolverPlugin,
        FakeTransportPlugin,
        SolverIngestPlugin,
    )
    storage: Storage = kernel.services.require(storage_contract.SERVICE_KEY)
    ts: TimeSeriesStore = kernel.services.require(ts_contract.SERVICE_KEY)

    # Create job+run in M1 tables, simulate temporary transport error on first monitor.polled
    up = await storage.upsert_job(
        "c1",
        JobInfo(
            job_id="55",
            name="conv",
            state=JobState.RUNNING,
            raw_state="RUNNING",
            work_dir="/case",
        ),
        active=True,
    )
    run_id = up.record.run_id
    assert run_id is not None

    fake_transport.fail_next = ClusterUnavailableError(
        "c1", ConnectionState.BACKOFF, "temporary disconnect"
    )
    await kernel.bus.emit(
        monitor_contract.POLLED_EVENT,
        PollReport(cluster_id="c1", ok=True, active_jobs=1, discovered=1),
    )

    # Poll loop recovers cleanly and populates timeseries + solver state
    async def _synced() -> bool:
        return (await ts.get_run_solver_state(run_id)) is not None

    await _wait_until(_synced)
    assert kernel.state("solver-ingest") is PluginState.ACTIVE
    series_before = await ts.list_series(run_id)
    assert len(series_before) > 0
    await kernel.stop()

    # Restarting kernel preserves durable state AND bootstraps the still-active run from DB
    kernel2 = Kernel({"storage-sqlite": {"path": db_path}})
    await kernel2.add(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        ConvergeSolverPlugin,
        SolverIngestPlugin,
    )
    await asyncio.sleep(0.05)
    ingestor2: Any = kernel2.services.require(ingest_contract.SERVICE_KEY)
    assert run_id in ingestor2._tracked
    ts2: TimeSeriesStore = kernel2.services.require(ts_contract.SERVICE_KEY)
    assert (await ts2.get_run_solver_state(run_id)) is not None
    assert len(await ts2.list_series(run_id)) == len(series_before)
    await kernel2.stop()


async def test_monitor_polled_uses_runs_and_events(tmp_path: Path) -> None:
    conv_session = LocalRemoteSession(
        FIXTURES_ROOT / "converge", cluster_id="c1", virtual_root="/case"
    )
    profiles = {"c1": ClusterProfile(host="cluster.example", user="user1")}
    fake_transport = _FakeTransport({"c1": conv_session}, clusters=profiles)

    class _FakeScheduler:
        kind = "slurm"

        def __init__(self) -> None:
            self.active_jobs = [
                JobInfo(
                    job_id="101",
                    name="conv_case",
                    state=JobState.RUNNING,
                    raw_state="RUNNING",
                    user="user1",
                    work_dir="/case",
                    stdout_path="/case/logout.txt",
                )
            ]
            self.finals: dict[str, JobInfo] = {}

        async def list_active(self, session: RemoteSession, user: str, tz: Any) -> list[JobInfo]:
            return list(self.active_jobs)

        async def final_states(
            self, session: RemoteSession, job_ids: Any, tz: Any
        ) -> dict[str, JobInfo]:
            return {jid: self.finals[jid] for jid in job_ids if jid in self.finals}

        def cancel_argv(self, job_id: str) -> list[str]:
            return ["scancel", job_id]

    scheduler = _FakeScheduler()

    class FakeTransportPlugin(Plugin):
        name = "fake-transport"
        provides = transport_contract.SERVICE_KEY
        contract_version = transport_contract.CONTRACT_VERSION

        def apply(self, ctx: Context) -> _FakeTransport:
            return fake_transport

    class FakeSchedulerPlugin(Plugin):
        name = "fake-scheduler"
        provides = f"{scheduler_contract.SERVICE_PREFIX}slurm"
        contract_version = scheduler_contract.CONTRACT_VERSION

        def apply(self, ctx: Context) -> _FakeScheduler:
            return scheduler

    db_path = str(tmp_path / "sim.db")
    kernel = Kernel(
        {
            "storage-sqlite": {"path": db_path},
            "monitor": {"loop": False},
            "solver-ingest": {"poll_interval_s": 60.0},
        }
    )
    await kernel.add(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        ConvergeSolverPlugin,
        FakeTransportPlugin,
        FakeSchedulerPlugin,
        MonitorPlugin,
        SolverIngestPlugin,
    )
    assert kernel.state("monitor") is PluginState.ACTIVE
    assert kernel.state("solver-ingest") is PluginState.ACTIVE

    monitor: Monitor = kernel.services.require(monitor_contract.SERVICE_KEY)
    storage: Storage = kernel.services.require(storage_contract.SERVICE_KEY)
    ts: TimeSeriesStore = kernel.services.require(ts_contract.SERVICE_KEY)

    # First monitor poll creates job + run in DB and emits monitor.polled
    report1 = await monitor.poll("c1")
    assert report1.discovered == 1

    jobs = await storage.jobs(cluster_id="c1")
    assert len(jobs) == 1
    run_id = jobs[0].run_id
    assert run_id is not None

    async def _run_ingested() -> bool:
        return (await ts.get_run_solver_state(run_id)) is not None

    await _wait_until(_run_ingested)
    state = await ts.get_run_solver_state(run_id)
    assert state is not None
    assert state.solver == "converge"
    assert len(await ts.list_series(run_id)) > 0

    # Job finishes on second poll -> final sync marks tracked run completed/removed
    scheduler.active_jobs = []
    scheduler.finals = {
        "101": JobInfo(
            job_id="101",
            name="conv_case",
            state=JobState.COMPLETED,
            raw_state="COMPLETED",
            user="user1",
            work_dir="/case",
            stdout_path="/case/logout.txt",
        )
    }
    report2 = await monitor.poll("c1")
    assert report2.changed == 1

    ingestor: Any = kernel.services.require(ingest_contract.SERVICE_KEY)

    async def _is_terminal_removed() -> bool:
        return run_id not in ingestor._tracked and run_id in ingestor._terminal_synced

    await _wait_until(_is_terminal_removed)
    await kernel.stop()


async def test_manual_runs_are_tracked_and_resumed(tmp_path: Path) -> None:
    session = LocalRemoteSession(FIXTURES_ROOT / "converge", cluster_id="c1", virtual_root="/case")
    fake_transport = _FakeTransport({"c1": session}, clusters={"c1": ClusterProfile(host="h")})

    class FakeTransportPlugin(Plugin):
        name = "fake-transport"
        provides = transport_contract.SERVICE_KEY
        contract_version = transport_contract.CONTRACT_VERSION

        def apply(self, ctx: Context) -> _FakeTransport:
            return fake_transport

    db_path = str(tmp_path / "sim.db")
    config = {"storage-sqlite": {"path": db_path}, "solver-ingest": {"poll_interval_s": 0.05}}

    # A manual run registered before the restart, plus one on an unknown cluster.
    kernel = Kernel(config)
    await kernel.add(SqliteStoragePlugin, SqliteTimeSeriesPlugin)
    storage: Storage = kernel.services.require(storage_contract.SERVICE_KEY)
    resumed = await storage.create_run(cluster_id="c1", work_dir="/case")
    orphan = await storage.create_run(cluster_id="gone", work_dir="/case")
    await kernel.stop()

    kernel = Kernel(config)
    try:
        await kernel.add(
            SqliteStoragePlugin,
            SqliteTimeSeriesPlugin,
            ConvergeSolverPlugin,
            FakeTransportPlugin,
            SolverIngestPlugin,
        )
        storage = kernel.services.require(storage_contract.SERVICE_KEY)
        ts: TimeSeriesStore = kernel.services.require(ts_contract.SERVICE_KEY)
        ingestor: IngestService = kernel.services.require(ingest_contract.SERVICE_KEY)

        async def done(run_id: int) -> bool:
            state = await ts.get_run_solver_state(run_id)
            return state is not None and state.solver_state is SolverState.COMPLETED

        await _wait_until(lambda: done(resumed.id))  # resumed by the poll loop
        assert await ts.get_run_solver_state(orphan.id) is None

        fresh = await storage.create_run(cluster_id="c1", work_dir="/case")
        await ingestor.track_run("c1", fresh.id, "/case", solver_kind="converge")
        await _wait_until(lambda: done(fresh.id))
        assert [r.id for r in await storage.runs(cluster_id="c1")] == [fresh.id, resumed.id]
    finally:
        await kernel.stop()


def _time_update(**kwargs: Any) -> Any:
    from simmanager.contracts.solver import SolverStatusUpdate

    return SolverStatusUpdate(**kwargs)


def test_time_derived_progress_advances_across_syncs() -> None:
    """Progress derived from start/end/current time must follow the solver time.

    Regression: once a first value was stored, later syncs reused it and the
    progress bar (and progress series) stayed frozen.
    """
    from simmanager.plugins.solver_ingest.plugin import _merge_run_state

    first = _merge_run_state(
        1,
        "openfoam",
        "/w",
        None,
        [_time_update(start_time=0.0, end_time=10.0), _time_update(current_time=2.0)],
    )
    assert first.progress == pytest.approx(0.2)

    second = _merge_run_state(1, "openfoam", "/w", first, [_time_update(current_time=5.0)])
    assert second.progress == pytest.approx(0.5)

    # A sync with no new solver time keeps the last value.
    idle = _merge_run_state(1, "openfoam", "/w", second, [])
    assert idle.progress == pytest.approx(0.5)


def test_explicit_progress_is_not_overridden_by_times() -> None:
    from simmanager.plugins.solver_ingest.plugin import _merge_run_state

    base = _merge_run_state(
        1, "converge", "/w", None, [_time_update(start_time=0.0, end_time=10.0)]
    )
    state = _merge_run_state(
        1, "converge", "/w", base, [_time_update(current_time=5.0, progress=0.42)]
    )
    assert state.progress == pytest.approx(0.42)
