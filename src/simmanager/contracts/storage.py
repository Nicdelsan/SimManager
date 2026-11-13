"""`storage` contract: durable facts (event log; jobs/runs/samples in later milestones).

Consumers depend on this Protocol, never on the SQLite implementation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

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
