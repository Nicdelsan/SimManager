"""Terminal sessions: one PTY each, a ring buffer for re-attach, orphan expiry.

Privacy: terminal input and output never reach a logger, the event log or an exception
message. Only the session's metadata (cluster, folder, duration) is recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import posixpath
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from simmanager.contracts import terminal as contract
from simmanager.contracts.storage import Storage
from simmanager.contracts.terminal import (
    Output,
    TerminalError,
    TerminalInfo,
    TerminalRequest,
    TerminalState,
)
from simmanager.contracts.transport import (
    ClusterUnavailableError,
    ConnectionState,
    PtyChannel,
    RemoteDirectoryMissingError,
    TerminalLimitError,
    TmuxSpec,
    Transport,
    TransportError,
)

SOURCE = "terminal"
READ_SIZE = 65536
HIGH_WATER = 64
"""Output chunks queued for a slow browser before the PTY stops being read (the SSH
window then fills and the remote program waits, as with a plain `ssh`)."""
_NEEDS_RECONNECT = (ConnectionState.AWAITING_AUTH, ConnectionState.BACKOFF, ConnectionState.FAILED)

Emit = Callable[[str, Any], Awaitable[None]]
Clock = Callable[[], float]


def tmux_prefix(cluster_id: str) -> str:
    return "simman-" + re.sub(r"[^A-Za-z0-9_-]", "_", cluster_id) + "-"


class RingBuffer:
    """Keeps the last `capacity` bytes of output."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._chunks: deque[bytes] = deque()
        self._size = 0

    def append(self, data: bytes) -> None:
        if len(data) >= self.capacity:
            self._chunks.clear()
            data = data[-self.capacity :]
            self._size = 0
        self._chunks.append(data)
        self._size += len(data)
        while self._size > self.capacity:
            excess = self._size - self.capacity
            first = self._chunks[0]
            if len(first) <= excess:
                self._chunks.popleft()
                self._size -= len(first)
            else:
                self._chunks[0] = first[excess:]
                self._size -= excess

    def snapshot(self) -> bytes:
        return b"".join(self._chunks)


class Attachment:
    """Implements `terminal.TerminalAttachment`."""

    def __init__(self, session: Session, replay: bytes) -> None:
        self._s = session
        self._replay = replay
        self._queue: deque[Output] = deque()
        self._ready = asyncio.Event()
        self._room = asyncio.Event()
        self._room.set()
        self.closed = False

    @property
    def replay(self) -> bytes:
        return self._replay

    async def recv(self) -> Output:
        while not self._queue:
            if self.closed:
                return None
            self._ready.clear()
            await self._ready.wait()
        item = self._queue.popleft()
        if len(self._queue) < HIGH_WATER // 2:
            self._room.set()
        return item

    async def write(self, data: bytes) -> None:
        if not self.closed and self._s.pty is not None and self._s.state is TerminalState.OPEN:
            await self._s.pty.write(data)

    def resize(self, cols: int, rows: int) -> None:
        if self.closed:
            return
        self._s.cols, self._s.rows = cols, rows
        if self._s.pty is not None:
            self._s.pty.resize(cols, rows)

    def detach(self) -> None:
        self._s.manager._detach(self._s, self)

    # -------------------------------------------------------------- session side
    def push(self, item: Output) -> None:
        if self.closed:
            return
        self._queue.append(item)
        if len(self._queue) >= HIGH_WATER:
            self._room.clear()
        self._ready.set()

    async def wait_room(self) -> None:
        while not self._room.is_set() and not self.closed:
            await self._room.wait()

    def end(self) -> None:
        """Final: the reader gets None after what is already queued."""
        self.closed = True
        self._room.set()
        self._ready.set()


@dataclasses.dataclass(eq=False)
class Session:
    manager: TerminalManager
    id: str
    request: TerminalRequest
    created_at: datetime
    cols: int
    rows: int
    buffer: RingBuffer
    state: TerminalState = TerminalState.CONNECTING
    detail: str = ""
    error: TerminalError | None = None
    exit_status: int | None = None
    tmux_session: str | None = None
    pty: PtyChannel | None = None
    attachment: Attachment | None = None
    detached_at: float | None = None
    opened_at: float | None = None
    task: asyncio.Task[None] | None = None

    def info(self) -> TerminalInfo:
        cwd = self.request.cwd
        where = posixpath.basename(cwd.rstrip("/")) if cwd else "~"
        return TerminalInfo(
            id=self.id,
            cluster_id=self.request.cluster_id,
            cwd=cwd,
            run_id=self.request.run_id,
            title=f"{self.request.cluster_id}:{where or cwd}",
            state=self.state,
            detail=self.detail,
            created_at=self.created_at,
            attached=self.attachment is not None,
            tmux_session=self.tmux_session,
            exit_status=self.exit_status,
            error=self.error,
        )


class TerminalManager:
    """Implements `terminal.TerminalService`."""

    def __init__(
        self,
        transport: Callable[[], Transport],
        storage: Storage,
        emit: Emit,
        log: logging.Logger,
        *,
        orphan_timeout_s: float,
        buffer_bytes: int,
        clock: Clock = time.monotonic,
    ) -> None:
        self._transport = transport
        self._storage = storage
        self._emit = emit
        self._log = log
        self.orphan_timeout_s = orphan_timeout_s
        self._buffer_bytes = buffer_bytes
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._connect_locks: dict[str, asyncio.Lock] = {}
        self._tmux_used: dict[str, int] = {}
        self._background: set[asyncio.Task[None]] = set()

    # ----------------------------------------------------------------- contract
    async def open(self, request: TerminalRequest) -> TerminalInfo:
        if request.cluster_id not in self._transport().clusters():
            raise KeyError(request.cluster_id)
        s = Session(
            manager=self,
            id=secrets.token_hex(8),
            request=request,
            created_at=datetime.now(UTC),
            cols=request.cols,
            rows=request.rows,
            buffer=RingBuffer(self._buffer_bytes),
            detached_at=self._clock(),
        )
        self._sessions[s.id] = s
        s.task = asyncio.get_running_loop().create_task(self._run(s), name=f"terminal-{s.id}")
        await self._changed(s)
        return s.info()

    def sessions(self) -> list[TerminalInfo]:
        return [s.info() for s in sorted(self._sessions.values(), key=lambda s: s.created_at)]

    def get(self, session_id: str) -> TerminalInfo | None:
        s = self._sessions.get(session_id)
        return None if s is None else s.info()

    def attach(self, session_id: str) -> Attachment:
        s = self._sessions[session_id]
        if s.attachment is not None:
            s.attachment.end()  # taken over by another tab
        # No await between the snapshot and the switch: no output can be lost or doubled.
        s.attachment = Attachment(s, s.buffer.snapshot())
        s.attachment.push(s.info())
        s.detached_at = None
        self._spawn(self._changed(s))
        return s.attachment

    async def close(self, session_id: str) -> None:
        await self._end(self._sessions[session_id], "user")

    async def tmux_sessions(self, cluster_id: str) -> list[str]:
        session = await self._transport().session(cluster_id)
        result = await session.run_readonly(["tmux", "list-sessions", "-F", "#{session_name}"])
        if not result.ok:  # "no server running": no sessions
            return []
        prefix = tmux_prefix(cluster_id)
        return sorted(
            (n for n in result.stdout.split() if n.startswith(prefix)),
            key=lambda n: (len(n), n),
        )

    # ---------------------------------------------------------------- lifecycle
    async def sweep(self) -> None:
        """Close sessions nobody has viewed for `orphan_timeout_s`."""
        now = self._clock()
        for s in list(self._sessions.values()):
            if s.detached_at is not None and now - s.detached_at >= self.orphan_timeout_s:
                await self._end(s, "expired")

    async def close_all(self) -> None:
        for s in list(self._sessions.values()):
            await self._end(s, "shutdown")

    def open_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.state is TerminalState.OPEN)

    # ------------------------------------------------------------------ session
    async def _run(self, s: Session) -> None:
        try:
            pty = await self._connect(s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._failed(s, exc)
            return
        s.pty = pty
        s.state, s.detail, s.opened_at = TerminalState.OPEN, "", self._clock()
        await self._changed(s)
        await self._event(contract.OPENED_EVENT, s, {"tmux_session": s.tmux_session})
        await self._pump(s, pty)
        await self._shell_ended(s, pty)

    async def _connect(self, s: Session) -> PtyChannel:
        transport = self._transport()
        cid = s.request.cluster_id
        lock = self._connect_locks.setdefault(cid, asyncio.Lock())
        async with lock:  # two tabs opened together share one reconnect attempt
            state = transport.state(cid)
            if state in _NEEDS_RECONNECT:
                # Opening a terminal is an explicit user action: one new attempt, which
                # may ask for a password/OTP in the browser. Never retried automatically.
                s.state, s.detail = TerminalState.AWAITING_AUTH, f"{cid} is {state}: reconnecting"
                await self._changed(s)
                await transport.reset(cid)
            remote = await transport.session(cid)
        s.state, s.detail = TerminalState.CONNECTING, "opening shell"
        await self._changed(s)
        tmux = await self._tmux_spec(s, remote)
        s.tmux_session = tmux.session if tmux else None
        return await remote.open_pty(cols=s.cols, rows=s.rows, cwd=s.request.cwd, tmux=tmux)

    async def _tmux_spec(self, s: Session, remote: Any) -> TmuxSpec | None:
        req = s.request
        if req.attach_tmux:
            return TmuxSpec(req.attach_tmux, attach=True)
        wanted = req.tmux
        if wanted is None:
            wanted = self._transport().clusters()[req.cluster_id].terminal_tmux
        if not wanted:
            return None
        prefix = tmux_prefix(req.cluster_id)
        highest = self._tmux_used.get(req.cluster_id, 0)
        with contextlib.suppress(TransportError):
            result = await remote.run_readonly(["tmux", "list-sessions", "-F", "#{session_name}"])
            for name in result.stdout.split() if result.ok else ():
                if name.startswith(prefix) and name[len(prefix) :].isdigit():
                    highest = max(highest, int(name[len(prefix) :]))
        self._tmux_used[req.cluster_id] = highest + 1
        return TmuxSpec(f"{prefix}{highest + 1}")

    async def _pump(self, s: Session, pty: PtyChannel) -> None:
        while True:
            data = await pty.read(READ_SIZE)
            if not data:
                return
            s.buffer.append(data)
            att = s.attachment
            if att is not None:
                att.push(data)
                await att.wait_room()

    async def _shell_ended(self, s: Session, pty: PtyChannel) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(pty.wait_closed(), 5)
        s.exit_status = pty.exit_status
        s.state = TerminalState.EXITED
        s.detail = (
            f"shell exited with status {s.exit_status}"
            if s.exit_status is not None
            else "connection closed"
        )
        await self._changed(s)
        await self._closed_event(s, "exited" if s.exit_status is not None else "connection_lost")

    async def _failed(self, s: Session, exc: Exception) -> None:
        if isinstance(exc, ClusterUnavailableError):
            error, detail = TerminalError.UNAVAILABLE, f"{exc.state}: {exc.detail}"
        elif isinstance(exc, TerminalLimitError):
            error, detail = TerminalError.LIMIT, str(exc)
        elif isinstance(exc, RemoteDirectoryMissingError):
            error, detail = TerminalError.MISSING_DIR, f"folder not found: {exc.path}"
        elif isinstance(exc, TransportError):
            error, detail = TerminalError.TRANSPORT, str(exc)
        else:
            self._log.error("terminal %s: unexpected %s", s.id, type(exc).__name__)
            error, detail = TerminalError.TRANSPORT, f"internal error ({type(exc).__name__})"
        s.state, s.detail, s.error = TerminalState.FAILED, detail, error
        await self._changed(s)

    async def _end(self, s: Session, reason: str) -> None:
        if self._sessions.pop(s.id, None) is None:
            return
        was_open = s.state is TerminalState.OPEN
        if s.pty is not None:
            s.pty.close()
        if s.task is not None and not s.task.done():
            s.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await s.task
        s.state, s.detail = TerminalState.CLOSED, reason
        if s.attachment is not None:
            s.attachment.push(s.info())
            s.attachment.end()
            s.attachment = None
        if was_open:
            if s.pty is not None:
                s.exit_status = s.pty.exit_status
            await self._closed_event(s, reason)
        await self._changed(s)

    def _detach(self, s: Session, att: Attachment) -> None:
        att.end()
        if s.attachment is att:
            s.attachment = None
            s.detached_at = self._clock()
            if s.id in self._sessions:
                self._spawn(self._changed(s))

    # ------------------------------------------------------------------ helpers
    async def _changed(self, s: Session) -> None:
        info = s.info()
        if s.attachment is not None:
            s.attachment.push(info)
        await self._emit(contract.STATE_EVENT, info)

    async def _closed_event(self, s: Session, reason: str) -> None:
        duration = self._clock() - s.opened_at if s.opened_at is not None else 0.0
        await self._event(
            contract.CLOSED_EVENT,
            s,
            {"reason": reason, "duration_s": round(duration, 1), "exit_status": s.exit_status},
        )

    async def _event(self, kind: str, s: Session, extra: dict[str, Any]) -> None:
        payload = {"session_id": s.id, "cwd": s.request.cwd, **extra}
        try:
            await self._storage.append_event(
                kind,
                payload,
                source=SOURCE,
                cluster_id=s.request.cluster_id,
                run_id=s.request.run_id,
            )
        except Exception as exc:
            self._log.error("terminal %s: cannot record %s (%s)", s.id, kind, type(exc).__name__)

    def _spawn(self, coro: Awaitable[None]) -> None:
        async def run() -> None:
            await coro

        task = asyncio.get_running_loop().create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
