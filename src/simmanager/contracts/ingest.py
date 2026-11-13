"""`ingest` contract: orchestrates solver discovery, incremental file reading and backfill."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .timeseries import SeriesKey, SolverState
from .transport import RemoteSession

SERVICE_KEY = "ingest"
CONTRACT_VERSION = 1

PROGRESS_SERIES = SeriesKey(
    file_path="@simmanager/progress",
    name="progress",
    unit="fraction",
    x_name="wall_time",
    x_unit="s since epoch (UTC)",
)
"""Run progress (0..1) against real time, stored in the `timeseries` store by ingest
whenever the progress changes. Used for the ETA and the progress plot."""

DURABLE_EVENT_KINDS = frozenset(
    {
        "run.solver_state_changed",
        "solver.finished",
        "solver.error",
        "solver.nan_detected",
        "file.truncated",
    }
)
"""Ingest facts written to the durable event log. Everything else ingest emits
(`series.updated`, `run.progress`, `backfill.progress`) is bus-only."""


@dataclass(frozen=True)
class SyncRunResult:
    """Summary of one ingestion pass for a run."""

    run_id: int
    solver: str | None = None
    files_synced: int = 0
    bytes_read: int = 0
    points_ingested: int = 0
    backfill_pending: int = 0
    solver_state: SolverState = SolverState.UNKNOWN
    progress: float | None = None
    watched_files: list[str] = field(default_factory=list)


class IngestService(Protocol):
    async def sync_run(
        self,
        session: RemoteSession,
        run_id: int,
        work_dir: str,
        *,
        solver_kind: str | None = None,
        stdout_path: str | None = None,
        run_backfill_immediately: bool = False,
    ) -> SyncRunResult:
        """Discover watched files for a run, read new chunks, and update durable state."""
        ...

    async def track_run(
        self,
        cluster_id: str,
        run_id: int,
        work_dir: str,
        *,
        solver_kind: str | None = None,
    ) -> None:
        """Follow a run that has no scheduler job (manual registration): sync it now and
        at every ingest poll until its solver state is terminal. Tracking resumes after a
        restart for manual runs whose solver state is not terminal."""
        ...
