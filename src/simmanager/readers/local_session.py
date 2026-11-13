"""Local `RemoteSession` implementation for tests and local directory demos."""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from pathlib import Path

from simmanager.contracts.transport import (
    ActionApproval,
    CommandNotAllowedError,
    CommandResult,
    PtyChannel,
    RemoteStat,
    TmuxSpec,
    TransportError,
)


class LocalRemoteSession:
    """Implements `RemoteSession` over a local directory tree (for tests and local demos)."""

    def __init__(
        self,
        root: Path,
        *,
        cluster_id: str = "local",
        user: str = "user1",
        virtual_root: str | None = None,
    ) -> None:
        self._root = root.resolve()
        self._cluster_id = cluster_id
        self._user = user
        self._virtual_root = (
            virtual_root.rstrip("/") if virtual_root is not None else self._root.as_posix()
        )

    @property
    def cluster_id(self) -> str:
        return self._cluster_id

    @property
    def user(self) -> str:
        return self._user

    @property
    def root_posix(self) -> str:
        return self._virtual_root

    def _resolve(self, remote_path: str) -> Path:
        norm = posixpath.normpath(remote_path.replace("\\", "/"))
        vroot = posixpath.normpath(self._virtual_root)
        if norm == vroot:
            rel = ""
        elif norm.startswith(vroot + "/"):
            rel = norm[len(vroot) + 1 :]
        elif not posixpath.isabs(norm):
            rel = norm
        else:
            # Allow direct host POSIX paths when virtual_root == root.as_posix()
            candidate = Path(remote_path)
            try:
                candidate.resolve().relative_to(self._root)
                return candidate.resolve()
            except ValueError as exc:
                raise PermissionError(f"path outside local session root: {remote_path}") from exc
        resolved = (self._root / rel).resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise PermissionError(f"path escapes local session root: {remote_path}") from exc
        return resolved

    async def run_readonly(self, argv: Sequence[str], *, timeout_s: float = 30) -> CommandResult:
        raise CommandNotAllowedError(f"LocalRemoteSession does not execute commands: {list(argv)}")

    async def run_mutating(
        self, argv: Sequence[str], *, approval: ActionApproval, timeout_s: float = 30
    ) -> CommandResult:
        raise CommandNotAllowedError(f"LocalRemoteSession does not execute commands: {list(argv)}")

    async def open_pty(
        self,
        *,
        cols: int,
        rows: int,
        cwd: str | None = None,
        term: str = "xterm-256color",
        tmux: TmuxSpec | None = None,
    ) -> PtyChannel:
        raise TransportError("LocalRemoteSession has no terminals")

    async def stat(self, path: str) -> RemoteStat | None:
        try:
            local = self._resolve(path)
        except PermissionError:
            return None
        if not local.exists():
            return None
        st = local.stat()
        return RemoteStat(
            path=path,
            size=st.st_size,
            mtime=st.st_mtime,
            is_dir=local.is_dir(),
        )

    async def listdir(self, path: str) -> list[RemoteStat]:
        local = self._resolve(path)
        if not local.is_dir():
            return []
        out: list[RemoteStat] = []
        base = path.rstrip("/")
        for entry in sorted(local.iterdir(), key=lambda p: p.name):
            st = entry.stat()
            out.append(
                RemoteStat(
                    path=f"{base}/{entry.name}" if base else entry.name,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    is_dir=entry.is_dir(),
                )
            )
        return out

    async def read_range(self, path: str, offset: int, max_bytes: int) -> bytes:
        if offset < 0 or max_bytes <= 0:
            return b""
        local = self._resolve(path)
        if not local.is_file():
            raise FileNotFoundError(path)
        with local.open("rb") as fh:
            fh.seek(offset)
            return fh.read(max_bytes)
