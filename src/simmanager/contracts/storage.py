"""`storage` contract: durable facts (event log, scheduler jobs and runs; samples in M2).

Consumers depend on this Protocol, never on the SQLite implementation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .scheduler import JobInfo

SERVICE_KEY = "storage"
CONTRACT_VERSION = 1


@dataclass(frozen=True)
class EventRecord:
    id: int
    ts: datetime
    kind: str
    source: str
    cluster_id: str | None
    run_id: int | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class JobRecord:
    """A job as stored: latest scheduler facts plus bookkeeping."""

    pk: int
    cluster_id: str
    run_id: int | None
    active: bool
    """True while the job is in the scheduler queue."""
    first_seen: datetime
    last_seen: datetime
    info: JobInfo


@dataclass(frozen=True)
class JobUpsert:
    """Outcome of `upsert_job`."""

    record: JobRecord
    created: bool
    """True when the job (and its run) did not exist before."""
    previous: JobInfo | None
    """Stored facts before this update; None when created."""


class Storage(Protocol):
    async def append_event(
        self,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        source: str,
        cluster_id: str | None = None,
        run_id: int | None = None,
    ) -> int:
        """Append a fact to the append-only event log. Returns its id."""
        ...

    async def events(
        self,
        *,
        since_id: int = 0,
        kinds: Sequence[str] | None = None,
        run_id: int | None = None,
        limit: int = 500,
    ) -> list[EventRecord]:
        """Events with id > since_id, oldest first."""
        ...

    async def latest_events(self, limit: int = 50) -> list[EventRecord]:
        """The most recent events, newest first."""
        ...

    async def upsert_job(self, cluster_id: str, info: JobInfo, *, active: bool) -> JobUpsert:
        """Insert or update a job by (cluster_id, job_id). A new job gets a run (origin
        `discovered`) in the same transaction. Unset fields of `info` keep stored values."""
        ...

    async def jobs(
        self,
        *,
        cluster_id: str | None = None,
        active: bool | None = None,
        limit: int = 500,
    ) -> list[JobRecord]:
        """Jobs, most recently submitted first."""
        ...
