"""`terminal` contract: interactive shells on the clusters, owned by the user (M4).

A terminal session wraps one `transport` PTY. It outlives the browser tab that shows it
(re-attach with a replay of recent output) but not SimManager. SimManager never writes
into a terminal by itself: only the attached browser does (ADR 0007).

Durable events (facts only, never terminal content): `terminal.opened`, `terminal.closed`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

SERVICE_KEY = "terminal"
CONTRACT_VERSION = 1

STATE_EVENT = "terminal.state"
"""Bus event (not durable) on every session change; payload: `TerminalInfo`."""
OPENED_EVENT = "terminal.opened"
CLOSED_EVENT = "terminal.closed"


class TerminalState(StrEnum):
    CONNECTING = "connecting"
    AWAITING_AUTH = "awaiting_auth"
    """The cluster needs a password/OTP/confirmation: answer the prompt in the browser."""
    OPEN = "open"
    EXITED = "exited"
    """The shell ended (or the connection dropped); output stays readable until closed."""
    FAILED = "failed"
    """The shell could not be opened; `error` tells why."""
    CLOSED = "closed"


class TerminalError(StrEnum):
    UNAVAILABLE = "unavailable"
    """Cluster not reachable or authentication not completed."""
    LIMIT = "limit"
    """`max_terminals` reached on that cluster."""
    MISSING_DIR = "missing_dir"
    """The start folder does not exist (any more); open in the home folder instead?"""
    TRANSPORT = "transport"


@dataclass(frozen=True)
class TerminalRequest:
    cluster_id: str
    cwd: str | None = None
    """Start folder; None = home."""
    run_id: int | None = None
    """Run the terminal was opened from (context for quick actions and events)."""
    cols: int = 80
    rows: int = 24
    tmux: bool | None = None
    """New tmux session; None = the cluster's `terminal_tmux` setting."""
    attach_tmux: str | None = None
    """Attach to this existing tmux session instead (from `tmux_sessions`)."""


@dataclass(frozen=True)
class TerminalInfo:
    id: str
    cluster_id: str
    cwd: str | None
    run_id: int | None
    title: str
    state: TerminalState
    detail: str
    created_at: datetime
    attached: bool
    tmux_session: str | None = None
    exit_status: int | None = None
    error: TerminalError | None = None


Output = bytes | TerminalInfo | None
"""What an attachment receives: terminal output, a state change, or None when the
attachment ended (session closed, or taken over by another tab)."""


class TerminalAttachment(Protocol):
    """The single live view of a session (a browser tab)."""

    @property
    def replay(self) -> bytes:
        """Recent output (up to the ring buffer size) at the moment of attaching."""
        ...

    async def recv(self) -> Output: ...

    async def write(self, data: bytes) -> None:
        """Keystrokes / pasted text from the user. Dropped while the shell is not open."""
        ...

    def resize(self, cols: int, rows: int) -> None: ...

    def detach(self) -> None:
        """Stop viewing; the session stays alive until `orphan_timeout_s`."""
        ...


class TerminalService(Protocol):
    async def open(self, request: TerminalRequest) -> TerminalInfo:
        """Start a session; connecting (and authenticating) continues in the background.

        Raises KeyError for an unknown cluster id."""
        ...

    def sessions(self) -> list[TerminalInfo]: ...

    def get(self, session_id: str) -> TerminalInfo | None: ...

    def attach(self, session_id: str) -> TerminalAttachment:
        """Replaces the current attachment, if any. Raises KeyError for unknown ids."""
        ...

    async def close(self, session_id: str) -> None:
        """End the session (the shell gets SIGHUP). Raises KeyError for unknown ids."""
        ...

    async def tmux_sessions(self, cluster_id: str) -> list[str]:
        """Live tmux sessions named `simman-<cluster>-<n>` on the cluster (read-only
        `tmux ls`). Raises `transport.ClusterUnavailableError` / `TransportError`."""
        ...
