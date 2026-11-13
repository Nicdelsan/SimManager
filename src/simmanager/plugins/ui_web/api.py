"""REST API under /api (JSON). Read-only except reconnect and manual run registration.

Services are looked up at each request: the UI keeps working (with 503 on the affected
endpoints) while e.g. the transport plugin is reloading.
"""

from __future__ import annotations

import asyncio
import contextlib
import posixpath
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import ingest as ingest_contract
from simmanager.contracts import solver as solver_contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts import timeseries as ts_contract
from simmanager.contracts import transport as transport_contract
from simmanager.contracts.ingest import IngestService
from simmanager.contracts.monitor import PollReport
from simmanager.contracts.storage import EventRecord, JobRecord, RunRecord, Storage
from simmanager.contracts.timeseries import (
    RunSolverState,
    SeriesMeta,
    SolverState,
    TimeSeriesStore,
)
from simmanager.contracts.transport import Transport, TransportError
from simmanager.kernel.services import ServiceView

from . import derive

SOURCE = "ui-web"
MIN_POINTS, MAX_POINTS = 200, 10_000
ETA_POINTS = 2000
_SOLVER_DONE = frozenset({SolverState.COMPLETED, SolverState.STOPPED, SolverState.FAILED})


# --------------------------------------------------------------------- state cache
@dataclass
class ClusterStatus:
    detail: str = ""
    state_at: float | None = None
    last_poll: dict[str, Any] | None = None


@dataclass
class ClusterStatusCache:
    """Last `transport.state` and `monitor.polled` per cluster (bus events, not stored)."""

    clusters: dict[str, ClusterStatus] = field(default_factory=dict)

    def on_transport_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self.clusters.setdefault(payload["cluster_id"], ClusterStatus())
        status.detail = payload.get("detail", "")
        status.state_at = time.time()
        return {"type": "cluster.state", **payload, "at": status.state_at}

    def on_polled(self, report: PollReport) -> dict[str, Any]:
        poll = {
            "ok": report.ok,
            "active_jobs": report.active_jobs,
            "discovered": report.discovered,
            "changed": report.changed,
            "error": report.error,
            "next_poll_s": report.next_poll_s,
            "at": time.time(),
        }
        self.clusters.setdefault(report.cluster_id, ClusterStatus()).last_poll = poll
        return {"type": "monitor.polled", "cluster_id": report.cluster_id, **poll}


# ------------------------------------------------------------------- JSON shapes
def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def job_json(job: JobRecord) -> dict[str, Any]:
    i = job.info
    return {
        "pk": job.pk,
        "cluster_id": job.cluster_id,
        "run_id": job.run_id,
        "active": job.active,
        "first_seen": _iso(job.first_seen),
        "last_seen": _iso(job.last_seen),
        "job_id": i.job_id,
        "name": i.name,
        "state": str(i.state),
        "raw_state": i.raw_state,
        "user": i.user,
        "work_dir": i.work_dir,
        "partition": i.partition,
        "nodes": i.nodes,
        "reason": i.reason,
        "exit_code": i.exit_code,
        "elapsed_s": i.elapsed_s,
        "time_limit_s": i.time_limit_s,
        "submit_time": _iso(i.submit_time),
        "start_time": _iso(i.start_time),
        "end_time": _iso(i.end_time),
    }


def solver_json(state: RunSolverState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "solver": state.solver,
        "state": str(state.solver_state),
        "progress": state.progress,
        "current_time": state.current_time,
        "start_time": state.start_time,
        "end_time": state.end_time,
        "ncyc": state.ncyc,
        "warning_count": state.warning_count,
        "error_message": state.error_message,
        "case_dir": state.case_dir,
        "output_dir": state.output_dir,
        "updated_at": _iso(state.updated_at),
    }


def series_json(s: SeriesMeta) -> dict[str, Any]:
    return {
        "id": s.id,
        "file_path": s.file_path,
        "name": s.name,
        "qualifier": s.qualifier,
        "unit": s.unit,
        "x_name": s.x_name,
        "x_unit": s.x_unit,
        "point_count": s.point_count,
        "last_x": s.last_x,
        "last_y": s.last_y,
        "min_y": s.min_y,
        "max_y": s.max_y,
        "progress": s.file_path == ingest_contract.PROGRESS_SERIES.file_path,
    }


def event_json(e: EventRecord) -> dict[str, Any]:
    return {
        "id": e.id,
        "ts": _iso(e.ts),
        "kind": e.kind,
        "source": e.source,
        "cluster_id": e.cluster_id,
        "run_id": e.run_id,
        "payload": e.payload,
    }


def eta_json(value: derive.Eta | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"rate_per_s": value.rate_per_s, "remaining_s": value.remaining_s, "at": value.at}


# ------------------------------------------------------------------------ bodies
class NewRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str = Field(min_length=1, max_length=200)
    work_dir: str = Field(min_length=2, max_length=4096)
    solver: Literal["converge", "openfoam"] | None = None
    """None = detect from the files in `work_dir`."""


# ---------------------------------------------------------------------------- API
class Api:
    def __init__(self, services: ServiceView, status: ClusterStatusCache) -> None:
        self.services = services
        self.status = status
        self._background: set[asyncio.Task[None]] = set()

    def _need(self, key: str) -> Any:
        service = self.services.get(key)
        if service is None:
            raise HTTPException(503, f"service {key!r} not available")
        return service

    @property
    def storage(self) -> Storage:
        return self._need(storage_contract.SERVICE_KEY)  # type: ignore[no-any-return]

    @property
    def ts(self) -> TimeSeriesStore:
        return self._need(ts_contract.SERVICE_KEY)  # type: ignore[no-any-return]

    @property
    def transport(self) -> Transport:
        return self._need(transport_contract.SERVICE_KEY)  # type: ignore[no-any-return]

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=f"ui-web:{name}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------- derived data
    async def run_eta(
        self, run_id: int, series: list[SeriesMeta] | None = None
    ) -> derive.Eta | None:
        if series is None:
            series = await self.ts.list_series(run_id)
        key = ingest_contract.PROGRESS_SERIES
        meta = next(
            (s for s in series if s.file_path == key.file_path and s.name == key.name), None
        )
        if meta is None or meta.point_count < 2:
            return None
        pts = await self.ts.query_series(meta.id, max_points=ETA_POINTS)
        return derive.eta(pts.x, pts.y)

    async def run_summary(self, run: RunRecord) -> dict[str, Any]:
        job = await self.storage.job_for_run(run.id)
        solver = await self.ts.get_run_solver_state(run.id)
        live = solver is None or solver.solver_state not in _SOLVER_DONE
        return {
            "id": run.id,
            "origin": run.origin,
            "cluster_id": run.cluster_id,
            "work_dir": run.work_dir,
            "created_at": _iso(run.created_at),
            "job": job_json(job) if job else None,
            "solver": solver_json(solver),
            "eta": eta_json(await self.run_eta(run.id)) if live else None,
        }


def build_router(api: Api) -> APIRouter:
    r = APIRouter()

    @r.get("/clusters")
    async def clusters() -> list[dict[str, Any]]:
        transport = api.transport
        out = []
        for cid, profile in transport.clusters().items():
            status = api.status.clusters.get(cid, ClusterStatus())
            out.append(
                {
                    "id": cid,
                    "target": profile.target(),
                    "scheduler": profile.scheduler,
                    "state": str(transport.state(cid)),
                    "detail": status.detail,
                    "state_at": status.state_at,
                    "last_poll": status.last_poll,
                }
            )
        return out

    @r.post("/clusters/{cluster_id}/reconnect", status_code=202)
    async def reconnect(cluster_id: str) -> dict[str, Any]:
        transport = api.transport
        if cluster_id not in transport.clusters():
            raise HTTPException(404, "unknown cluster")
        await transport.reset(cluster_id)

        async def connect() -> None:
            # May ask for a password/OTP through auth.prompter; failures end up in the
            # transport state (and its events), not here.
            with contextlib.suppress(TransportError):
                await transport.session(cluster_id)

        api.spawn(connect(), f"reconnect-{cluster_id}")
        return {"cluster_id": cluster_id, "state": str(transport.state(cluster_id))}

    @r.get("/jobs")
    async def jobs(
        cluster: str | None = None,
        active: bool | None = None,
        limit: int = Query(200, ge=1, le=2000),
    ) -> list[dict[str, Any]]:
        records = await api.storage.jobs(cluster_id=cluster, active=active, limit=limit)
        return [job_json(j) for j in records]

    @r.get("/runs")
    async def runs(
        cluster: str | None = None, limit: int = Query(100, ge=1, le=1000)
    ) -> list[dict[str, Any]]:
        records = await api.storage.runs(cluster_id=cluster, limit=limit)
        return [await api.run_summary(run) for run in records]

    @r.post("/runs", status_code=201)
    async def create_run(body: NewRun) -> dict[str, Any]:
        transport = api.transport
        if body.cluster_id not in transport.clusters():
            raise HTTPException(404, "unknown cluster")
        work_dir = body.work_dir.strip()
        if not work_dir.startswith("/") or any(c in work_dir for c in "\0\r\n"):
            raise HTTPException(422, "work_dir must be an absolute POSIX path on the cluster")
        work_dir = posixpath.normpath(work_dir)
        if body.solver and api.services.get(solver_contract.SERVICE_PREFIX + body.solver) is None:
            raise HTTPException(422, f"solver {body.solver!r} not available")
        ingest: IngestService = api._need(ingest_contract.SERVICE_KEY)
        storage = api.storage
        run = await storage.create_run(work_dir=work_dir, cluster_id=body.cluster_id)
        await storage.append_event(
            "run.created",
            {"origin": "manual", "work_dir": work_dir, "solver": body.solver},
            source=SOURCE,
            cluster_id=body.cluster_id,
            run_id=run.id,
        )
        await ingest.track_run(body.cluster_id, run.id, work_dir, solver_kind=body.solver)
        return await api.run_summary(run)

    @r.get("/runs/{run_id}")
    async def run_detail(run_id: int) -> dict[str, Any]:
        run = await api.storage.run(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        summary = await api.run_summary(run)
        series = await api.ts.list_series(run_id)
        cursors = await api.ts.list_cursors(run_id)
        summary["series"] = [series_json(s) for s in series]
        summary["files"] = [
            {
                "file_path": c.file_path,
                "parser": c.parser_kind,
                "size": c.file_size,
                "status": str(c.status),
                "read_fraction": derive.read_fraction(c),
                "backfill_pending": c.needs_backfill,
            }
            for c in cursors
        ]
        return summary

    @r.get("/runs/{run_id}/series")
    async def run_series(run_id: int) -> list[dict[str, Any]]:
        return [series_json(s) for s in await api.ts.list_series(run_id)]

    @r.get("/series/{series_id}/points")
    async def points(
        series_id: int,
        x_min: float | None = None,
        x_max: float | None = None,
        width: int = Query(1000, ge=1, le=10_000),
    ) -> dict[str, Any]:
        max_points = max(MIN_POINTS, min(MAX_POINTS, 2 * width))
        try:
            pts = await api.ts.query_series(
                series_id, x_min=x_min, x_max=x_max, max_points=max_points
            )
        except (KeyError, LookupError) as exc:
            raise HTTPException(404, "unknown series") from exc
        return {
            "series_id": pts.series_id,
            "x": pts.x,
            "y": pts.y,
            "level": pts.level,
            "total_points": pts.total_points,
        }

    @r.get("/events")
    async def events(
        limit: int = Query(100, ge=1, le=1000), run_id: int | None = None
    ) -> list[dict[str, Any]]:
        storage = api.storage
        if run_id is None:
            records = await storage.latest_events(limit)
        else:
            records = list(reversed(await storage.events(run_id=run_id, limit=10_000)))[:limit]
        return [event_json(e) for e in records]

    return r
