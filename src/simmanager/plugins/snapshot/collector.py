"""Remote input collection, large-file hash reuse, and streaming SHA-256."""

from __future__ import annotations

import fnmatch
import hashlib
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from simmanager.contracts.snapshot import ContentStatus, SnapshotFileSpec
from simmanager.contracts.transport import RemoteSession, RemoteStat, TransportError


@dataclass(frozen=True)
class CollectorConfig:
    max_file_bytes: int = 1_048_576
    hash_large_files: bool = True
    hash_max_bytes: int = 104_857_600
    read_chunk_bytes: int = 512 * 1024
    converge_globs: tuple[str, ...] = ("*.in", "*.dat")
    openfoam_roots: tuple[str, ...] = ("system", "constant", "0", "0.orig")
    openfoam_exclude_dirs: tuple[str, ...] = ("polyMesh", "triSurface", "*Mesh*")


@dataclass(frozen=True)
class CollectedInput:
    rel_path: str
    remote_path: str
    stat: RemoteStat


@dataclass
class PendingHash:
    rel_path: str
    remote_path: str
    size_bytes: int
    mtime: float


def compute_manifest_sha256(files: Sequence[SnapshotFileSpec]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda x: x.rel_path):
        h.update(f"{f.rel_path}\0{f.sha256 or ''}\0{f.size_bytes}\0{f.content_status}\n".encode())
    return h.hexdigest()


def _matches_any(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _detect_text(raw: bytes) -> tuple[bool, str]:
    if b"\x00" in raw[:8192]:
        return False, "binary"
    try:
        raw.decode("utf-8")
        return True, "utf-8"
    except UnicodeDecodeError:
        return True, "latin-1"


async def detect_solver_kind(
    session: RemoteSession, work_dir: str, hint: str | None = None
) -> str | None:
    if hint in ("converge", "openfoam"):
        return hint
    wdir = posixpath.normpath(work_dir).rstrip("/")
    if (
        await session.stat(f"{wdir}/inputs.in") is not None
        or await session.stat(f"{wdir}/logout.txt") is not None
    ):
        return "converge"
    if await session.stat(f"{wdir}/system/controlDict") is not None:
        return "openfoam"

    entries = await session.listdir(wdir)
    names = {posixpath.basename(e.path): e for e in entries}
    if any(not e.is_dir and n.endswith(".in") for n, e in names.items()):
        return "converge"
    if any(e.is_dir and n in ("system", "constant", "0") for n, e in names.items()):
        return "openfoam"
    return None


async def discover_input_files(
    session: RemoteSession,
    work_dir: str,
    solver: str | None,
    cfg: CollectorConfig,
) -> list[CollectedInput]:
    wdir = posixpath.normpath(work_dir).rstrip("/")
    found: list[CollectedInput] = []

    if solver == "converge":
        for entry in await session.listdir(wdir):
            if entry.is_dir:
                continue
            name = posixpath.basename(entry.path)
            if _matches_any(name, cfg.converge_globs):
                found.append(CollectedInput(rel_path=name, remote_path=entry.path, stat=entry))
    elif solver == "openfoam":
        for root_name in cfg.openfoam_roots:
            root_path = f"{wdir}/{root_name}"
            st = await session.stat(root_path)
            if st is None or not st.is_dir:
                continue
            await _walk_dir(session, root_path, root_name, cfg.openfoam_exclude_dirs, found)
    else:
        # Generic fallback: top-level .in/.dat plus system/constant/0 if present
        for entry in await session.listdir(wdir):
            name = posixpath.basename(entry.path)
            if not entry.is_dir and _matches_any(name, cfg.converge_globs):
                found.append(CollectedInput(rel_path=name, remote_path=entry.path, stat=entry))
            elif entry.is_dir and name in cfg.openfoam_roots:
                await _walk_dir(session, entry.path, name, cfg.openfoam_exclude_dirs, found)

    found.sort(key=lambda item: item.rel_path)
    return found


async def _walk_dir(
    session: RemoteSession,
    remote_dir: str,
    rel_prefix: str,
    exclude_dirs: Sequence[str],
    out: list[CollectedInput],
) -> None:
    for entry in await session.listdir(remote_dir):
        name = posixpath.basename(entry.path)
        rel = f"{rel_prefix}/{name}"
        if entry.is_dir:
            if _matches_any(name, exclude_dirs):
                continue
            await _walk_dir(session, entry.path, rel, exclude_dirs, out)
        else:
            out.append(CollectedInput(rel_path=rel, remote_path=entry.path, stat=entry))


async def read_remote_bytes(
    session: RemoteSession, remote_path: str, size_bytes: int, chunk_bytes: int
) -> bytes:
    buf = bytearray()
    offset = 0
    while offset < size_bytes:
        chunk = await session.read_range(remote_path, offset, min(chunk_bytes, size_bytes - offset))
        if not chunk:
            break
        buf.extend(chunk)
        offset += len(chunk)
    return bytes(buf)


async def stream_sha256_remote(
    session: RemoteSession, remote_path: str, size_bytes: int, chunk_bytes: int
) -> str:
    h = hashlib.sha256()
    offset = 0
    while offset < size_bytes:
        chunk = await session.read_range(remote_path, offset, min(chunk_bytes, size_bytes - offset))
        if not chunk:
            break
        h.update(chunk)
        offset += len(chunk)
    return h.hexdigest()


def check_late_files(
    inputs: Sequence[CollectedInput], job_start_time: datetime | None
) -> tuple[bool, tuple[str, ...]]:
    if job_start_time is None:
        return False, ()
    start_ts = job_start_time.timestamp()
    # ASSUMPTION(A300): Remote SFTP mtime and job StartTime (cluster tz -> UTC) share NTP sync
    # within 1 second; mtime > start_ts indicates a file modified after the job started.
    late = tuple(item.rel_path for item in inputs if item.stat.mtime > start_ts + 1.0)
    return bool(late), late


async def read_and_prepare_file(
    session: RemoteSession,
    item: CollectedInput,
    cfg: CollectorConfig,
    *,
    reused_sha256: str | None = None,
) -> tuple[SnapshotFileSpec, tuple[bytes, bool, str] | None, PendingHash | None]:
    st = item.stat
    if st.size > cfg.max_file_bytes:
        if reused_sha256 is not None:
            return (
                SnapshotFileSpec(
                    rel_path=item.rel_path,
                    size_bytes=st.size,
                    mtime=st.mtime,
                    sha256=reused_sha256,
                    content_status=ContentStatus.OVERSIZE_HASHED,
                    is_text=False,
                    encoding="binary",
                ),
                None,
                None,
            )
        pending = (
            PendingHash(item.rel_path, item.remote_path, st.size, st.mtime)
            if cfg.hash_large_files and st.size <= cfg.hash_max_bytes
            else None
        )
        return (
            SnapshotFileSpec(
                rel_path=item.rel_path,
                size_bytes=st.size,
                mtime=st.mtime,
                sha256=None,
                content_status=ContentStatus.OVERSIZE_UNHASHED,
                is_text=False,
                encoding="binary",
            ),
            None,
            pending,
        )

    try:
        raw = await read_remote_bytes(session, item.remote_path, st.size, cfg.read_chunk_bytes)
    except (OSError, TransportError):
        return (
            SnapshotFileSpec(
                rel_path=item.rel_path,
                size_bytes=st.size,
                mtime=st.mtime,
                sha256=None,
                content_status=ContentStatus.UNREADABLE,
                is_text=False,
                encoding="binary",
            ),
            None,
            None,
        )

    sha = hashlib.sha256(raw).hexdigest()
    is_text, encoding = _detect_text(raw)
    return (
        SnapshotFileSpec(
            rel_path=item.rel_path,
            size_bytes=len(raw),
            mtime=st.mtime,
            sha256=sha,
            content_status=ContentStatus.STORED,
            is_text=is_text,
            encoding=encoding,
        ),
        (raw, is_text, encoding),
        None,
    )
