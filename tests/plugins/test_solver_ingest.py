from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from simmanager.contracts import ingest as ingest_contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts import timeseries as ts_contract
from simmanager.contracts import transport as transport_contract
from simmanager.contracts.clusters import ClusterProfile
from simmanager.contracts.ingest import IngestService
from simmanager.contracts.storage import Storage
from simmanager.contracts.timeseries import SolverState, TimeSeriesStore
from simmanager.contracts.transport import (
    ClusterUnavailableError,
    ConnectionState,
    RemoteSession,
)
from simmanager.kernel import Context, Kernel, Plugin, PluginState
from simmanager.plugins.solver_converge import ConvergeSolverPlugin
from simmanager.plugins.solver_ingest import SolverIngestPlugin
from simmanager.plugins.solver_openfoam import OpenFoamSolverPlugin
from simmanager.plugins.storage_sqlite import SqliteStoragePlugin, SqliteTimeSeriesPlugin
from simmanager.readers import LocalRemoteSession

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures"


class _FakeTransport:
    def __init__(self, sessions: dict[str, RemoteSession]) -> None:
        self._sessions = sessions
        self.fail_next: ClusterUnavailableError | None = None

    def clusters(self) -> Mapping[str, ClusterProfile]:
        return {}

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

    conv_session = LocalRemoteSession(FIXTURES_ROOT / "converge")
    res_conv = await ingestor.sync_run(conv_session, run_id=1, work_dir=conv_session.root_posix)
    assert res_conv.solver == "converge"
    assert res_conv.files_synced >= 3
    assert res_conv.points_ingested > 0
    assert res_conv.solver_state is SolverState.COMPLETED
    assert res_conv.progress == pytest.approx(1.0)

    of_session = LocalRemoteSession(FIXTURES_ROOT / "openfoam")
    res_of = await ingestor.sync_run(of_session, run_id=2, work_dir=of_session.root_posix)
    assert res_of.solver == "openfoam"
    assert res_of.points_ingested > 0
    assert res_of.solver_state is SolverState.COMPLETED
    assert res_of.progress == pytest.approx(1.0)

    ev_kinds = {e.kind for e in await storage.events(run_id=1)}
    assert "series.updated" in ev_kinds
    assert "run.progress" in ev_kinds

    series_conv = await ts.list_series(1)
    assert any(s.file_path == "thermo.out" for s in series_conv)
    assert (await ts.get_run_solver_state(2)) is not None
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
    ts: TimeSeriesStore = kernel.services.require(ts_contract.SERVICE_KEY)

    backfill_events: list[dict[str, Any]] = []
    kernel.bus.on("backfill.progress", lambda p: backfill_events.append(dict(p)))

    session = LocalRemoteSession(case_dir, virtual_root="/case")
    res = await ingestor.sync_run(session, run_id=5, work_dir="/case")
    assert res.backfill_pending == 1

    async def _is_backfill_done() -> bool:
        cursors = await ts.list_cursors(5)
        return bool(cursors) and not any(c.needs_backfill for c in cursors)

    await _wait_until(_is_backfill_done)
    assert any(e["done"] is True for e in backfill_events)

    series = await ts.list_series(5)
    temp = next(s for s in series if s.name == "Mean_Temp")
    assert temp.point_count == 45_000
    await kernel.stop()


async def test_m1_bus_events_and_transport_resilience(tmp_path: Path) -> None:
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
    ts: TimeSeriesStore = kernel.services.require(ts_contract.SERVICE_KEY)

    # First run.created fails transport temporarily -> plugin must stay ACTIVE
    fake_transport.fail_next = ClusterUnavailableError(
        "c1", ConnectionState.BACKOFF, "temporary disconnect"
    )
    await kernel.bus.emit(
        "run.created",
        {"run_id": 10, "cluster_id": "c1", "work_dir": "/case"},
    )
    await asyncio.sleep(0.05)
    assert kernel.state("solver-ingest") is PluginState.ACTIVE

    # Poll loop / subsequent job.state_changed recovers cleanly and reaches terminal removal
    await kernel.bus.emit(
        "job.state_changed",
        {"run_id": 10, "cluster_id": "c1", "work_dir": "/case", "to": "completed"},
    )

    async def _synced() -> bool:
        return (await ts.get_run_solver_state(10)) is not None

    await _wait_until(_synced)
    assert kernel.state("solver-ingest") is PluginState.ACTIVE
    series_before = await ts.list_series(10)
    assert len(series_before) > 0

    # Unloading solver-ingest preserves durable timeseries state
    await kernel.stop()

    kernel2 = Kernel({"storage-sqlite": {"path": db_path}})
    await kernel2.add(SqliteStoragePlugin, SqliteTimeSeriesPlugin)
    ts2: TimeSeriesStore = kernel2.services.require(ts_contract.SERVICE_KEY)
    assert (await ts2.get_run_solver_state(10)) is not None
    assert len(await ts2.list_series(10)) == len(series_before)
    await kernel2.stop()
