"""`monitor` contract: job discovery on the configured clusters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

SERVICE_KEY = "monitor"
CONTRACT_VERSION = 1

POLLED_EVENT = "monitor.polled"
"""Bus event after every poll of a cluster; payload is a PollReport."""


@dataclass(frozen=True)
class PollReport:
    cluster_id: str
    ok: bool
    active_jobs: int = 0
    discovered: int = 0
    changed: int = 0
    """State changes, including jobs that ended."""
    error: str | None = None
    next_poll_s: float = 0.0


class Monitor(Protocol):
    async def poll(self, cluster_id: str) -> PollReport:
        """Poll one cluster now. Never raises for cluster/scheduler problems."""
        ...

    async def poll_all(self) -> list[PollReport]:
        """Poll every monitored cluster concurrently."""
        ...
