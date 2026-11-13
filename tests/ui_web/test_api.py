"""REST API against a real server, real SQLite storage and the anonymized solver fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from simmanager.contracts import storage as storage_contract
from simmanager.contracts import transport as transport_contract
from simmanager.contracts.clusters import ClusterProfile
from simmanager.contracts.monitor import POLLED_EVENT, PollReport
from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.contracts.storage import Storage
from simmanager.contracts.transport import ConnectionState, RemoteSession
from simmanager.kernel import Context, Plugin
from simmanager.plugins.solver_converge import ConvergeSolverPlugin
from simmanager.plugins.solver_ingest import SolverIngestPlugin
from simmanager.plugins.solver_openfoam import OpenFoamSolverPlugin
from simmanager.plugins.storage_sqlite import SqliteStoragePlugin, SqliteTimeSeriesPlugin
from simmanager.readers import LocalRemoteSession

from .webclient import Web, running_web

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class FakeTransport:
    def __init__(self, sessions: dict[str, RemoteSession]) -> None:
        self.sessions = sessions
        self.resets: list[str] = []
        self.session_calls: list[str] = []

    def clusters(self) -> Mapping[str, ClusterProfile]:
        return {cid: ClusterProfile(host=f"{cid}.example.org", user="user1") for cid in self.sessions}

    async def session(self, cluster_id: str) -> RemoteSession:
        self.session_calls.append(cluster_id)
        return self.sessions[cluster_id]

    def state(self, cluster_id: str) -> ConnectionState:
        return ConnectionState.CONNECTED

    async def reset(self, cluster_id: str) -> None:
        self.resets.append(cluster_id)


TRANSPORT = FakeTransport(
    {"c1": LocalRemoteSession(FIXTURES / "converge", cluster_id="c1", virtual_root="/case")}
)


class FakeTransportPlugin(Plugin):
    name = "fake-transport"
    provides = transport_contract.SERVICE_KEY
    contract_version = transport_contract.CONTRACT_VERSION

    def apply(self, ctx: Context) -> FakeTransport:
        return TRANSPORT


@pytest.fixture
async def web(tmp_path: Path) -> AsyncIterator[Web]:
    TRANSPORT.resets.clear()
    TRANSPORT.session_calls.clear()
    config = {
        "storage-sqlite": {"path": str(tmp_path / "sim.db")},
        "solver-ingest": {"poll_interval_s": 0.05},
    }
    async with running_web(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        ConvergeSolverPlugin,
        OpenFoamSolverPlugin,
        FakeTransportPlugin,
        SolverIngestPlugin,
        config=config,
    ) as w:
        yield w


async def _get(web: Web, path: str, **params: Any) -> Any:
    async with web.client() as c:
        r = await c.get(path, params=params)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


async def _wait_run_done(web: Web, run_id: int) -> dict[str, Any]:
    for _ in range(250):
        detail = await _get(web, f"/api/runs/{run_id}")
        if detail["solver"] and detail["solver"]["state"] == "completed":
            return detail  # type: ignore[no-any-return]
        await asyncio.sleep(0.02)
    raise AssertionError("run not synced")


async def test_manual_run_registration_and_detail(web: Web) -> None:
    r = await web.post_json("/api/runs", {"cluster_id": "c1", "work_dir": "/case/"})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["origin"] == "manual" and created["work_dir"] == "/case"
    detail = await _wait_run_done(web, created["id"])

    assert detail["solver"]["solver"] == "converge"
    assert detail["solver"]["progress"] == pytest.approx(1.0)
    assert detail["eta"] is None  # done: no ETA
    assert detail["files"] and all(
        f["read_fraction"] == pytest.approx(1.0) for f in detail["files"]
    )
    names = {s["name"] for s in detail["series"]}
    assert "progress" in names and len(names) > 1
    assert next(s for s in detail["series"] if s["progress"])["unit"] == "fraction"

    runs = await _get(web, "/api/runs")
    assert [x["id"] for x in runs] == [created["id"]]
    events = await _get(web, "/api/events", run_id=created["id"])
    kinds = [e["kind"] for e in events]
    assert "run.created" in kinds and "solver.finished" in kinds
    assert kinds[0] != "run.created"  # newest first


async def test_manual_run_validation(web: Web) -> None:
    for body, status in (
        ({"cluster_id": "nope", "work_dir": "/case"}, 404),
        ({"cluster_id": "c1", "work_dir": "relative/dir"}, 422),
        ({"cluster_id": "c1", "work_dir": "/case\nrm"}, 422),
        ({"cluster_id": "c1", "work_dir": "/case", "solver": "fluent"}, 422),
        ({"cluster_id": "c1", "work_dir": "/case", "extra": 1}, 422),
    ):
        r = await web.post_json("/api/runs", body)
        assert r.status_code == status, (body, r.text)
    assert await _get(web, "/api/runs") == []


async def test_series_points_downsampling(web: Web) -> None:
    r = await web.post_json("/api/runs", {"cluster_id": "c1", "work_dir": "/case"})
    detail = await _wait_run_done(web, r.json()["id"])
    biggest = max(detail["series"], key=lambda s: s["point_count"])
    full = await _get(web, f"/api/series/{biggest['id']}/points", width=5000)
    assert full["level"] == 0 and len(full["x"]) == biggest["point_count"] == full["total_points"]
    assert full["x"] == sorted(full["x"])
    window = await _get(
        web, f"/api/series/{biggest['id']}/points", x_min=full["x"][1], x_max=full["x"][-2]
    )
    assert window["x"][0] <= full["x"][1] and window["x"][-1] >= full["x"][-2]
    async with web.client() as c:
        assert (await c.get("/api/series/999999/points")).status_code == 404
        assert (await c.get("/api/runs/999999")).status_code == 404
        assert (await c.get("/api/series/1/points", params={"width": 0})).status_code == 422


async def test_clusters_jobs_and_reconnect(web: Web) -> None:
    storage: Storage = web.kernel.services.require(storage_contract.SERVICE_KEY)
    await storage.upsert_job(
        "c1",
        JobInfo(job_id="42", name="spray", state=JobState.RUNNING, raw_state="RUNNING"),
        active=True,
    )
    await web.kernel.bus.emit(
        POLLED_EVENT, PollReport(cluster_id="c1", ok=True, active_jobs=1, next_poll_s=60)
    )
    await web.kernel.bus.emit(
        transport_contract.STATE_EVENT,
        {"cluster_id": "c1", "state": "connected", "detail": "ok"},
    )
    clusters = await _get(web, "/api/clusters")
    assert clusters[0]["id"] == "c1" and clusters[0]["state"] == "connected"
    assert clusters[0]["target"] == "user1@c1.example.org"
    assert clusters[0]["last_poll"]["active_jobs"] == 1 and clusters[0]["detail"] == "ok"

    jobs = await _get(web, "/api/jobs", active=True)
    assert [(j["job_id"], j["state"]) for j in jobs] == [("42", "running")]
    runs = await _get(web, "/api/runs")
    assert runs[0]["job"]["job_id"] == "42" and runs[0]["origin"] == "discovered"

    r = await web.post_json("/api/clusters/c1/reconnect")
    assert r.status_code == 202
    assert TRANSPORT.resets == ["c1"]
    for _ in range(100):
        if "c1" in TRANSPORT.session_calls:
            break
        await asyncio.sleep(0.01)
    assert "c1" in TRANSPORT.session_calls
    assert (await web.post_json("/api/clusters/zz/reconnect")).status_code == 404


async def test_api_without_services_answers_503() -> None:
    async with running_web() as web, web.client() as c:
        assert (await c.get("/api/runs")).status_code == 503
        assert (await c.get("/api/clusters")).status_code == 503
