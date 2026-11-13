"""`transport` contract: remote command execution and file reads on a cluster.

The monitoring engine only uses `run_readonly` (allowlisted, read-only commands) and the file
reads. Anything that changes the cluster goes through `run_mutating`, which requires an
explicit user approval and is written to the event log (ADR 0004).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .clusters import ClusterProfile

SERVICE_KEY = "transport"
CONTRACT_VERSION = 1

STATE_EVENT = "transport.state"
"""Bus event emitted on every connection state change: {cluster_id, state, detail}."""


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AWAITING_AUTH = "awaiting_auth"
    """Human input is needed (expired certificate, password, OTP). No automatic retry."""
    CONNECTED = "connected"
    BACKOFF = "backoff"
    """Network-level failure; retried automatically after a growing delay."""
    FAILED = "failed"
    """Needs a configuration fix (unknown cluster, unverifiable host key). No retry."""


class TransportError(Exception):
    """Base class of transport failures."""


class ClusterUnavailableError(TransportError):
    """The cluster cannot be reached now. `state` tells why; `retry_at` when to try again."""

    def __init__(
        self,
        cluster_id: str,
        state: ConnectionState,
        detail: str,
        retry_at: datetime | None = None,
    ) -> None:
        super().__init__(f"{cluster_id}: {state} - {detail}")
        self.cluster_id = cluster_id
        self.state = state
        self.detail = detail
        self.retry_at = retry_at


class CommandNotAllowedError(TransportError):
    """`run_readonly` was asked to run something outside the read-only allowlist."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


@dataclass(frozen=True)
class RemoteStat:
    path: str
    size: int
    mtime: float
    """POSIX timestamp."""
    is_dir: bool


@dataclass(frozen=True)
class ActionApproval:
    """Proof that the user explicitly confirmed a mutating action (e.g. in a UI dialog)."""

    action: str
    """Short identifier, e.g. `job.cancel`."""
    approved_by: str
    """Where the confirmation came from, e.g. `cli` or `web:<session>`."""
    reason: str = ""


class RemoteSession(Protocol):
    """An authenticated connection to one cluster. Cheap to obtain; do not cache it."""

    @property
    def cluster_id(self) -> str: ...

    @property
    def user(self) -> str:
        """Remote login name actually used for this connection."""
        ...

    async def run_readonly(self, argv: Sequence[str], *, timeout_s: float = 30) -> CommandResult:
        """Run an allowlisted read-only command. Raises CommandNotAllowedError otherwise."""
        ...

    async def run_mutating(
        self, argv: Sequence[str], *, approval: ActionApproval, timeout_s: float = 30
    ) -> CommandResult:
        """Run a command that changes the cluster. Logged as action.executed/action.result."""
        ...

    async def stat(self, path: str) -> RemoteStat | None:
        """None when the path does not exist."""
        ...

    async def listdir(self, path: str) -> list[RemoteStat]: ...

    async def read_range(self, path: str, offset: int, max_bytes: int) -> bytes:
        """Up to `max_bytes` starting at `offset` (fewer at end of file)."""
        ...


class Transport(Protocol):
    def clusters(self) -> Mapping[str, ClusterProfile]:
        """Configured cluster profiles by id."""
        ...

    async def session(self, cluster_id: str) -> RemoteSession:
        """Connect if needed (may ask the user via `auth.prompter`).

        Raises ClusterUnavailableError while the cluster is in backoff, awaiting authentication
        or failed; KeyError for an unknown cluster id.
        """
        ...

    def state(self, cluster_id: str) -> ConnectionState: ...

    async def reset(self, cluster_id: str) -> None:
        """Forget a backoff/auth/failed state and close the connection: next session() retries."""
        ...
