r"""SSH transport: one asyncssh connection per cluster, opened lazily and shared.

Connection state machine (per cluster):

    DISCONNECTED -> CONNECTING -> CONNECTED
                              \-> BACKOFF        network error; retried after 5 s .. 5 min
                              \-> AWAITING_AUTH  needs a human (expired certificate, password,
                                                 OTP, passphrase); retried only after reset()
                                                 or when key/certificate files change on disk
                              \-> FAILED         configuration problem (host key, bad key file)
    CONNECTED -> DISCONNECTED when the connection drops; the next session() reconnects.

Authentication never retries blindly in the background: a rejected login would otherwise be
repeated at every poll and could lock the account.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import posixpath
import random
import re
import stat as stat_mod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncssh

from simmanager.contracts.auth import AuthPrompter, AuthRequest, Prompt
from simmanager.contracts.clusters import ClusterProfile
from simmanager.contracts.storage import Storage
from simmanager.contracts.transport import (
    ActionApproval,
    ClusterUnavailableError,
    CommandResult,
    ConnectionState,
    RemoteStat,
    TransportError,
)

from .certs import CertInfo, certs_for, files_fingerprint
from .policy import check_readonly, to_command_line

log = logging.getLogger("simmanager.plugin.transport-ssh")

SOURCE = "transport-ssh"
BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 300.0
RENEW_TIMEOUT_S = 300.0

Emit = Callable[[str, Any], Awaitable[None]]
PrompterLookup = Callable[[], AuthPrompter | None]


class _NeedsAuthError(Exception):
    """Internal: a human must act before the next attempt."""


class _Cluster:
    def __init__(self, cluster_id: str, profile: ClusterProfile) -> None:
        self.id = cluster_id
        self.profile = profile
        self.lock = asyncio.Lock()
        self.sftp_lock = asyncio.Lock()
        self.channels = asyncio.Semaphore(profile.max_channels)
        self.state = ConnectionState.DISCONNECTED
        self.detail = ""
        self.conn: asyncssh.SSHClientConnection | None = None
        self.sftp: asyncssh.SFTPClient | None = None
        self.failures = 0
        self.retry_at: datetime | None = None
        self.auth_fingerprint: tuple[tuple[str, float], ...] | None = None


class SshTransport:
    """Implements `simmanager.contracts.transport.Transport`."""

    def __init__(
        self,
        profiles: Mapping[str, ClusterProfile],
        *,
        storage: Storage,
        emit: Emit,
        prompter: PrompterLookup,
    ) -> None:
        self._clusters = {cid: _Cluster(cid, p) for cid, p in profiles.items()}
        self._storage = storage
        self._emit = emit
        self._prompter = prompter
        self._background: set[asyncio.Task[None]] = set()

    # ----------------------------------------------------------------- contract
    def clusters(self) -> Mapping[str, ClusterProfile]:
        return {cid: c.profile for cid, c in self._clusters.items()}

    def state(self, cluster_id: str) -> ConnectionState:
        return self._clusters[cluster_id].state

    async def session(self, cluster_id: str) -> SshSession:
        c = self._clusters[cluster_id]
        async with c.lock:
            if c.conn is None:
                self._check_may_connect(c)
                await self._connect(c)
            assert c.conn is not None
            return SshSession(self, c, c.conn)

    async def reset(self, cluster_id: str) -> None:
        c = self._clusters[cluster_id]
        async with c.lock:
            await self._close_conn(c)
            c.failures = 0
            c.retry_at = None
            c.auth_fingerprint = None
            await self._set_state(c, ConnectionState.DISCONNECTED, "reset")

    async def close(self) -> None:
        for c in self._clusters.values():
            async with c.lock:
                await self._close_conn(c)
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    # -------------------------------------------------------------- connecting
    def _check_may_connect(self, c: _Cluster) -> None:
        if c.state is ConnectionState.FAILED:
            raise ClusterUnavailableError(c.id, c.state, c.detail)
        if c.state is ConnectionState.BACKOFF and c.retry_at and datetime.now(UTC) < c.retry_at:
            raise ClusterUnavailableError(c.id, c.state, c.detail, c.retry_at)
        if c.state is ConnectionState.AWAITING_AUTH:
            if c.auth_fingerprint == files_fingerprint(c.profile.key_files):
                raise ClusterUnavailableError(c.id, c.state, c.detail)
            log.info("%s: key/certificate files changed, retrying authentication", c.id)

    async def _connect(self, c: _Cluster) -> None:
        await self._set_state(c, ConnectionState.CONNECTING, c.profile.target())
        try:
            await self._ensure_valid_certs(c)
            conn = await self._open(c)
        except _NeedsAuthError as exc:
            await self._fail_auth(c, str(exc))
        except asyncssh.PermissionDenied as exc:
            await self._fail_auth(c, f"authentication rejected ({exc.reason})")
        except asyncssh.HostKeyNotVerifiable as exc:
            await self._fail(
                c,
                f"host key not verifiable ({exc.reason}). Connect once with `ssh` to record it "
                "in known_hosts, or set `known_hosts` in clusters.yaml",
            )
        except (asyncssh.KeyImportError, asyncssh.ConfigParseError, FileNotFoundError) as exc:
            await self._fail(c, f"SSH configuration problem: {exc}{_path_hint(exc)}")
        except (OSError, TimeoutError, asyncssh.Error) as exc:
            await self._backoff(c, f"{type(exc).__name__}: {exc}".strip(": "))
        else:
            c.conn = conn
            c.failures = 0
            c.retry_at = None
            c.auth_fingerprint = None
            user = conn.get_extra_info("username")
            await self._set_state(c, ConnectionState.CONNECTED, f"as {user}")
            await self._event("cluster.connected", c, {"user": user})

    async def _open(self, c: _Cluster) -> asyncssh.SSHClientConnection:
        host, kwargs = connect_options(c.profile)
        kwargs["client_factory"] = lambda: _Client(self, c)
        # A key passphrase is asked when needed and dropped after the attempt: never kept
        # for reconnections (secrets must not stay in memory after use).
        for attempt in range(3):
            try:
                return await asyncssh.connect(host, **kwargs)
            except (asyncssh.KeyImportError, asyncssh.KeyEncryptionError) as exc:
                if not _is_passphrase_problem(exc):
                    raise
                retry = " (previous one was wrong)" if attempt else ""
                answers = await self._ask(
                    AuthRequest(
                        cluster_id=c.id,
                        kind="passphrase",
                        title=f"{c.id}: passphrase for the SSH key{retry}",
                        prompts=(Prompt("Key passphrase: "),),
                    )
                )
                if not answers:
                    msg = "the SSH key is encrypted and no passphrase was given"
                    raise _NeedsAuthError(msg) from exc
                kwargs["passphrase"] = answers[0]
        raise _NeedsAuthError("wrong SSH key passphrase")

    async def _ensure_valid_certs(self, c: _Cluster) -> None:
        expired = [ci for ci in certs_for(c.profile.key_files) if ci.expired()]
        if not expired:
            return
        cert = expired[0]
        when = cert.valid_before.astimezone().strftime("%Y-%m-%d %H:%M")
        command = c.profile.cert_renew_command
        if command and await self._confirm(
            c.id,
            f"The SSH certificate for {c.id} expired on {when}. "
            f"Run the renew command now? ({' '.join(command)})",
        ):
            await self._renew(c, command, cert)
            if not any(ci.expired() for ci in certs_for(c.profile.key_files)):
                return
            raise _NeedsAuthError("the certificate is still expired after the renew command")
        hint = " (set cert_renew_command to renew from SimManager)" if not command else ""
        raise _NeedsAuthError(f"SSH certificate expired on {when}; renew it{hint}")

    async def _renew(self, c: _Cluster, command: list[str], cert: CertInfo) -> None:
        log.info("%s: running certificate renew command", c.id)
        await self._event("cluster.cert_renew_started", c, {"certificate": cert.path.name})
        try:
            proc = await asyncio.create_subprocess_exec(*command)
            code = await asyncio.wait_for(proc.wait(), RENEW_TIMEOUT_S)
        except (OSError, TimeoutError) as exc:
            await self._event("cluster.cert_renew_failed", c, {"error": str(exc)})
            raise _NeedsAuthError(f"renew command failed: {exc}") from exc
        await self._event("cluster.cert_renew_finished", c, {"exit_status": code})

    # ------------------------------------------------------------- state changes
    async def _fail_auth(self, c: _Cluster, detail: str) -> None:
        c.auth_fingerprint = files_fingerprint(c.profile.key_files)
        await self._set_state(c, ConnectionState.AWAITING_AUTH, detail)
        await self._event("cluster.auth_required", c, {"detail": detail})
        raise ClusterUnavailableError(c.id, c.state, detail)

    async def _fail(self, c: _Cluster, detail: str) -> None:
        await self._set_state(c, ConnectionState.FAILED, detail)
        await self._event("cluster.failed", c, {"detail": detail})
        raise ClusterUnavailableError(c.id, c.state, detail)

    async def _backoff(self, c: _Cluster, detail: str) -> None:
        c.failures += 1
        delay = min(BACKOFF_MIN_S * 2 ** (c.failures - 1), BACKOFF_MAX_S)
        delay *= random.uniform(0.8, 1.2)
        c.retry_at = datetime.now(UTC) + timedelta(seconds=delay)
        await self._set_state(c, ConnectionState.BACKOFF, detail)
        if c.failures == 1:  # one event per outage, not one per attempt
            await self._event("cluster.unreachable", c, {"detail": detail})
        raise ClusterUnavailableError(c.id, c.state, detail, c.retry_at)

    async def _set_state(self, c: _Cluster, state: ConnectionState, detail: str) -> None:
        changed = state is not c.state
        c.state, c.detail = state, detail
        if changed:
            log.info("%s: %s %s", c.id, state, detail)
            await self._emit(
                "transport.state", {"cluster_id": c.id, "state": str(state), "detail": detail}
            )

    async def _event(self, kind: str, c: _Cluster, payload: Mapping[str, Any]) -> None:
        await self._storage.append_event(kind, payload, source=SOURCE, cluster_id=c.id)

    def _on_connection_lost(self, c: _Cluster, conn: Any, exc: Exception | None) -> None:
        if c.conn is not conn:
            return  # closed on purpose, or an old connection
        c.conn, c.sftp = None, None

        async def report() -> None:
            detail = str(exc) if exc else "closed by server"
            await self._set_state(c, ConnectionState.DISCONNECTED, detail)
            await self._event("cluster.connection_lost", c, {"detail": detail})

        task = asyncio.get_running_loop().create_task(report())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _close_conn(self, c: _Cluster) -> None:
        conn, c.conn, c.sftp = c.conn, None, None
        if conn is not None:
            conn.close()
            with contextlib.suppress(Exception):
                await conn.wait_closed()

    # ------------------------------------------------------------------ prompts
    async def _ask(self, request: AuthRequest) -> list[str] | None:
        prompter = self._prompter()
        return None if prompter is None else await prompter.ask(request)

    async def _confirm(self, cluster_id: str, question: str) -> bool:
        prompter = self._prompter()
        return False if prompter is None else await prompter.confirm(cluster_id, question)


def _path_hint(exc: Exception) -> str:
    r"""The SSH config is parsed with shell-like quoting: `C:\Users\me\key` becomes
    `C:Usersmekey`. Recognize that mangled shape and say how to fix it."""
    name = getattr(exc, "filename", None)
    if (
        isinstance(exc, FileNotFoundError)
        and isinstance(name, str)
        and re.match(r"^[A-Za-z]:[^\\/]", name)
    ):
        return (
            ". Windows paths in the SSH config need forward slashes (C:/Users/...); "
            "or set key_files in clusters.yaml"
        )
    return ""


def _is_passphrase_problem(exc: Exception) -> bool:
    """Missing passphrase ("Passphrase must be specified ...") or a wrong one ("Unable to
    decrypt ...", "Incorrect passphrase"), as opposed to e.g. a missing bcrypt package."""
    msg = str(exc).lower()
    return "bcrypt" not in msg and ("passphrase" in msg or "decrypt" in msg)


def connect_options(p: ClusterProfile) -> tuple[str, dict[str, Any]]:
    """Host and `asyncssh.connect` keyword arguments for a profile (no credentials)."""
    host = p.host or p.ssh_config_host
    if not host:
        raise asyncssh.ConfigParseError("profile needs `host` or `ssh_config_host`")
    kwargs: dict[str, Any] = {
        "keepalive_interval": p.keepalive_s,
        "connect_timeout": p.connect_timeout_s,
        "preferred_auth": list(p.auth),
        "compression_algs": ["zlib@openssh.com", "zlib", "none"] if p.compression else ["none"],
    }
    config = Path(p.ssh_config_file or Path.home() / ".ssh" / "config").expanduser()
    if p.ssh_config_file or config.is_file():
        kwargs["config"] = [str(config)]
    if p.port:
        kwargs["port"] = p.port
    if p.user:
        kwargs["username"] = p.user
    if p.key_files:
        kwargs["client_keys"] = [str(Path(k).expanduser()) for k in p.key_files]
        kwargs["agent_path"] = None  # like IdentitiesOnly: offer exactly the configured keys
    if p.known_hosts:
        kwargs["known_hosts"] = str(Path(p.known_hosts).expanduser())
    return host, kwargs


class _Client(asyncssh.SSHClient):
    """Forwards interactive authentication to the `auth.prompter` service."""

    def __init__(self, transport: SshTransport, cluster: _Cluster) -> None:
        self._transport = transport
        self._cluster = cluster
        self._conn: Any = None

    def connection_made(self, conn: asyncssh.SSHClientConnection) -> None:
        self._conn = conn

    def connection_lost(self, exc: Exception | None) -> None:
        self._transport._on_connection_lost(self._cluster, self._conn, exc)

    async def password_auth_requested(self) -> str | None:
        answers = await self._transport._ask(
            AuthRequest(
                cluster_id=self._cluster.id,
                kind="password",
                title=f"{self._cluster.id}: password for {self._cluster.profile.target()}",
                prompts=(Prompt("Password: "),),
            )
        )
        return answers[0] if answers else None

    def kbdint_auth_requested(self) -> str:
        return ""  # let the server choose the sub-methods (e.g. OTP)

    async def kbdint_challenge_received(
        self, name: str, instructions: str, lang: str, prompts: Sequence[tuple[str, bool]]
    ) -> list[str] | None:
        if not prompts:
            return []
        return await self._transport._ask(
            AuthRequest(
                cluster_id=self._cluster.id,
                kind="keyboard-interactive",
                title=f"{self._cluster.id}: {name or 'login'}",
                instructions=instructions,
                prompts=tuple(Prompt(text, echo) for text, echo in prompts),
            )
        )


class SshSession:
    """Implements `simmanager.contracts.transport.RemoteSession` on a live connection."""

    def __init__(
        self, transport: SshTransport, cluster: _Cluster, conn: asyncssh.SSHClientConnection
    ) -> None:
        self._t = transport
        self._c = cluster
        self._conn = conn

    @property
    def cluster_id(self) -> str:
        return self._c.id

    @property
    def user(self) -> str:
        return str(self._conn.get_extra_info("username"))

    async def run_readonly(self, argv: Sequence[str], *, timeout_s: float = 30) -> CommandResult:
        check_readonly(argv)
        return await self._run(argv, timeout_s)

    async def run_mutating(
        self, argv: Sequence[str], *, approval: ActionApproval, timeout_s: float = 30
    ) -> CommandResult:
        await self._t._event(
            "action.executed",
            self._c,
            {
                "argv": list(argv),
                "action": approval.action,
                "approved_by": approval.approved_by,
                "reason": approval.reason,
            },
        )
        try:
            result = await self._run(argv, timeout_s)
        except TransportError as exc:
            await self._t._event(
                "action.result", self._c, {"action": approval.action, "error": str(exc)}
            )
            raise
        await self._t._event(
            "action.result",
            self._c,
            {
                "action": approval.action,
                "exit_status": result.exit_status,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            },
        )
        return result

    async def _run(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
        async with self._c.channels:
            try:
                proc = await self._conn.run(
                    to_command_line(argv),
                    check=False,
                    timeout=timeout_s,
                    encoding="utf-8",
                    errors="replace",
                )
            except asyncssh.TimeoutError as exc:
                raise TransportError(f"{argv[0]}: timed out after {timeout_s:g} s") from exc
            except (asyncssh.Error, OSError) as exc:
                raise TransportError(f"{argv[0]}: {exc}") from exc
        status = proc.exit_status if proc.exit_status is not None else -1
        return CommandResult(tuple(argv), status, str(proc.stdout or ""), str(proc.stderr or ""))

    # -------------------------------------------------------------------- SFTP
    async def _sftp(self) -> asyncssh.SFTPClient:
        async with self._c.sftp_lock:
            if self._c.sftp is None or self._c.conn is not self._conn:
                try:
                    self._c.sftp = await self._conn.start_sftp_client()
                except (asyncssh.Error, OSError) as exc:
                    raise TransportError(f"SFTP unavailable: {exc}") from exc
            return self._c.sftp

    async def stat(self, path: str) -> RemoteStat | None:
        sftp = await self._sftp()
        try:
            attrs = await sftp.stat(path)
        except asyncssh.SFTPNoSuchFile:
            return None
        except (asyncssh.Error, OSError) as exc:
            raise TransportError(f"stat {path}: {exc}") from exc
        return _remote_stat(path, attrs)

    async def listdir(self, path: str) -> list[RemoteStat]:
        sftp = await self._sftp()
        try:
            names = await sftp.readdir(path)
        except (asyncssh.Error, OSError) as exc:
            raise TransportError(f"listdir {path}: {exc}") from exc
        return sorted(
            (
                _remote_stat(posixpath.join(path, str(n.filename)), n.attrs)
                for n in names
                if n.filename not in (".", "..")
            ),
            key=lambda s: s.path,
        )

    async def read_range(self, path: str, offset: int, max_bytes: int) -> bytes:
        sftp = await self._sftp()
        try:
            async with sftp.open(path, "rb") as f:
                data = await f.read(max_bytes, offset)
        except (asyncssh.Error, OSError) as exc:
            raise TransportError(f"read {path}: {exc}") from exc
        assert isinstance(data, bytes)  # binary mode
        return data


def _remote_stat(path: str, attrs: asyncssh.SFTPAttrs) -> RemoteStat:
    mode = attrs.permissions or 0
    return RemoteStat(
        path=path,
        size=int(attrs.size or 0),
        mtime=float(attrs.mtime or 0),
        is_dir=stat_mod.S_ISDIR(mode),
    )
