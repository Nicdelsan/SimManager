"""`scheduler.<kind>` contract (e.g. `scheduler.slurm`): batch scheduler queries.

A scheduler is stateless with respect to connections: every call receives the
`RemoteSession` to use. It only builds read-only commands and parses their output; the
cancel command is returned as argv so that the caller runs it via `run_mutating`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import StrEnum
from typing import Protocol

from .transport import RemoteSession

SERVICE_PREFIX = "scheduler."
CONTRACT_VERSION = 1


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.TIMEOUT, JobState.UNKNOWN}
)


@dataclass(frozen=True)
class JobInfo:
    """A scheduler job as seen at one moment. Times are timezone-aware or None."""

    job_id: str
    """Scheduler id as text; array tasks look like `123_4`."""
    name: str
    state: JobState
    raw_state: str
    user: str | None = None
    work_dir: str | None = None
    partition: str | None = None
    nodes: int | None = None
    node_list: str | None = None
    reason: str | None = None
    exit_code: str | None = None
    elapsed_s: int | None = None
    time_limit_s: int | None = None
    """None means unlimited or unknown."""
    submit_time: datetime | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    stdout_path: str | None = None


class Scheduler(Protocol):
    kind: str

    async def list_active(
        self, session: RemoteSession, user: str, tz: tzinfo | None
    ) -> list[JobInfo]:
        """Jobs of `user` currently known to the scheduler queue (pending/running/...).

        Raises TransportError if the queue cannot be read: an empty list always means
        "no jobs", never "could not tell".
        """
        ...

    async def final_states(
        self, session: RemoteSession, job_ids: Sequence[str], tz: tzinfo | None
    ) -> dict[str, JobInfo]:
        """Best-effort details of jobs that left the queue. Missing ids could not be found."""
        ...

    def cancel_argv(self, job_id: str) -> list[str]:
        """Command that cancels a job, to run via `RemoteSession.run_mutating`."""
        ...
