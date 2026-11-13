"""`ingest` contract: orchestrates solver discovery, incremental file reading and backfill."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .timeseries import SolverState
from .transport import RemoteSession

SERVICE_KEY = "ingest"
CONTRACT_VERSION = 1


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
