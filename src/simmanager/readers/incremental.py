"""Incremental remote file reader with truncation detection and tail-first backfill."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from simmanager.contracts.solver import ParsedChunk, Solver, SolverStatusUpdate, WatchedFile
from simmanager.contracts.timeseries import FileCursor, FileStatus, TimeSeriesStore
from simmanager.contracts.transport import RemoteSession


@dataclass(frozen=True)
class ReaderConfig:
    """Chunking and auto-backfill thresholds."""

    chunk_bytes: int = 4 * 1024 * 1024
    """Chunk size per `read_range` call (4-8 MB)."""
    full_read_max_bytes: int = 50 * 1024 * 1024
    """Files up to this size are read sequentially from start to end."""
    tail_preview_bytes: int = 20 * 1024 * 1024
    """For files > `full_read_max_bytes`, read this much of the tail immediately."""
    header_probe_bytes: int = 64 * 1024
    """Initial prefix read to parse headers/preamble on large files."""
    head_fingerprint_bytes: int = 256
    """Prefix length compared to detect truncation or in-place replacement."""


@dataclass(frozen=True)
class SyncFileResult:
    """Summary of one incremental sync pass on a file."""

    file_path: str
    bytes_read: int = 0
    points_ingested: int = 0
    truncated: bool = False
    needs_backfill: bool = False
    status_updates: list[SolverStatusUpdate] = field(default_factory=list)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


@dataclass(frozen=True)
class BackfillProgress:
    """Progress of one backfill step."""

    file_path: str
    bytes_read: int
    points_ingested: int
    remaining_bytes: int
    total_gap_bytes: int
    done: bool
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    warning_delta: int = 0


def _split_complete_lines(buf: bytes) -> tuple[bytes, int]:
    """Return `(complete_lines_bytes, consumed_byte_len)` up to the last `\\n`."""
    idx = buf.rfind(b"\n")
    if idx < 0:
        return b"", 0
    end = idx + 1
    return buf[:end], end


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


class IncrementalReader:
    """Coordinates `RemoteSession` chunked reads, solver parsing, and `TimeSeriesStore`."""

    def __init__(self, config: ReaderConfig | None = None) -> None:
        self._cfg = config or ReaderConfig()
        self._file_locks: dict[tuple[int, str], asyncio.Lock] = {}

    @property
    def config(self) -> ReaderConfig:
        return self._cfg

    def _lock_for(self, run_id: int, file_path: str) -> asyncio.Lock:
        key = (run_id, file_path)
        lock = self._file_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._file_locks[key] = lock
        return lock

    async def sync_file(
        self,
        session: RemoteSession,
        store: TimeSeriesStore,
        solver: Solver,
        run_id: int,
        watched: WatchedFile,
    ) -> SyncFileResult:
        """Read new bytes (or initial full / tail-first preview) from `watched`."""
        async with self._lock_for(run_id, watched.file_path):
            stat = await session.stat(watched.remote_path)
            if stat is None or stat.is_dir:
                return SyncFileResult(file_path=watched.file_path)

            cursor = await store.get_cursor(run_id, watched.file_path)
            truncated = False
            events: list[tuple[str, dict[str, Any]]] = []

            if stat.size == 0:
                if cursor is not None and cursor.forward_offset > 0:
                    await store.reset_file_data(run_id, watched.file_path)
                    truncated = True
                    events.append(
                        (
                            "file.truncated",
                            {"file_path": watched.file_path, "reason": "size_zero"},
                        )
                    )
                await store.upsert_cursor(
                    FileCursor(
                        run_id=run_id,
                        file_path=watched.file_path,
                        parser_kind=watched.parser_kind,
                        file_size=0,
                        mtime=stat.mtime,
                        head_prefix=b"",
                        forward_offset=0,
                        backfill_start=0,
                        backfill_end=0,
                        status=FileStatus.TRUNCATED if truncated else FileStatus.ACTIVE,
                    )
                )
                return SyncFileResult(
                    file_path=watched.file_path, truncated=truncated, events=events
                )

            fp_len = min(stat.size, self._cfg.head_fingerprint_bytes)
            new_head = await session.read_range(watched.remote_path, 0, fp_len)

            if cursor is not None:
                old_head = cursor.head_prefix
                cmp_len = min(len(old_head), len(new_head))
                is_smaller = stat.size < cursor.forward_offset
                head_changed = cmp_len > 0 and old_head[:cmp_len] != new_head[:cmp_len]
                if is_smaller or head_changed:
                    await store.reset_file_data(run_id, watched.file_path)
                    truncated = True
                    reason = "size_shrank" if is_smaller else "header_changed"
                    events.append(
                        ("file.truncated", {"file_path": watched.file_path, "reason": reason})
                    )
                    cursor = None

            if cursor is None:
                res = await self._initial_sync(
                    session, store, solver, run_id, watched, stat.size, stat.mtime, new_head
                )
                return SyncFileResult(
                    file_path=res.file_path,
                    bytes_read=res.bytes_read,
                    points_ingested=res.points_ingested,
                    truncated=truncated,
                    needs_backfill=res.needs_backfill,
                    status_updates=res.status_updates,
                    events=[*events, *res.events],
                )

            if stat.size <= cursor.forward_offset:
                return SyncFileResult(
                    file_path=watched.file_path,
                    needs_backfill=cursor.needs_backfill,
                )

            return await self._incremental_forward(
                session, store, solver, cursor, watched, stat.size, stat.mtime, new_head
            )

    async def _initial_sync(
        self,
        session: RemoteSession,
        store: TimeSeriesStore,
        solver: Solver,
        run_id: int,
        watched: WatchedFile,
        file_size: int,
        mtime: float,
        head_prefix: bytes,
    ) -> SyncFileResult:
        probe_len = min(file_size, self._cfg.header_probe_bytes)
        probe = await session.read_range(watched.remote_path, 0, probe_len)
        header_data, header_end = solver.parse_header(watched.file_path, watched.parser_kind, probe)

        if file_size <= self._cfg.full_read_max_bytes:
            # Sequential full read from 0 (or header_end if header was already consumed).
            cursor = FileCursor(
                run_id=run_id,
                file_path=watched.file_path,
                parser_kind=watched.parser_kind,
                file_size=file_size,
                mtime=mtime,
                head_prefix=head_prefix,
                forward_offset=0,
                backfill_start=0,
                backfill_end=0,
                header_data=header_data,
                parser_state={},
                status=FileStatus.ACTIVE,
            )
            return await self._read_forward_range(
                session, store, solver, cursor, watched, file_size, mtime, head_prefix
            )

        # Large file (> full_read_max_bytes): parse preamble up to header_end, then jump to tail!
        bytes_read = 0
        points_ingested = 0
        status_updates: list[SolverStatusUpdate] = []
        events: list[tuple[str, dict[str, Any]]] = []
        fwd_state: dict[str, Any] = {}

        if header_end > 0:
            preamble = probe[:header_end]
            parsed = solver.parse_chunk(
                watched.file_path,
                watched.parser_kind,
                _decode(preamble),
                header_data=header_data,
                parser_state=fwd_state,
                is_backfill=False,
            )
            bytes_read += len(preamble)
            points_ingested += await self._persist_parsed(store, run_id, parsed)
            fwd_state = dict(parsed.parser_state)
            if parsed.status_update is not None:
                status_updates.append(parsed.status_update)
            events.extend(parsed.events)

        tail_seek = max(header_end, file_size - self._cfg.tail_preview_bytes)
        tail_aligned = await self._find_aligned_offset(
            session, solver, watched, tail_seek, file_size
        )
        if tail_aligned is None:
            tail_aligned = tail_seek

        cursor = FileCursor(
            run_id=run_id,
            file_path=watched.file_path,
            parser_kind=watched.parser_kind,
            file_size=file_size,
            mtime=mtime,
            head_prefix=head_prefix,
            forward_offset=tail_aligned,
            backfill_start=header_end,
            backfill_end=tail_aligned,
            header_data=header_data,
            parser_state={"forward": fwd_state, "backfill": {}},
            status=FileStatus.BACKFILLING if header_end < tail_aligned else FileStatus.ACTIVE,
        )
        tail_res = await self._read_forward_range(
            session, store, solver, cursor, watched, file_size, mtime, head_prefix
        )
        return SyncFileResult(
            file_path=watched.file_path,
            bytes_read=bytes_read + tail_res.bytes_read,
            points_ingested=points_ingested + tail_res.points_ingested,
            needs_backfill=tail_res.needs_backfill,
            status_updates=[*status_updates, *tail_res.status_updates],
            events=[*events, *tail_res.events],
        )

    async def _find_aligned_offset(
        self,
        session: RemoteSession,
        solver: Solver,
        watched: WatchedFile,
        start_offset: int,
        file_size: int,
    ) -> int | None:
        pos = start_offset
        while pos < file_size:
            window = await session.read_range(
                watched.remote_path, pos, min(self._cfg.chunk_bytes, file_size - pos)
            )
            if not window:
                return None
            rel = solver.find_tail_alignment(watched.parser_kind, window)
            if rel is not None:
                return pos + rel
            pos += len(window)
        return None

    async def _incremental_forward(
        self,
        session: RemoteSession,
        store: TimeSeriesStore,
        solver: Solver,
        cursor: FileCursor,
        watched: WatchedFile,
        file_size: int,
        mtime: float,
        head_prefix: bytes,
    ) -> SyncFileResult:
        # Refresh header if initial read had too few bytes for a full header.
        header_data = cursor.header_data
        if not header_data:
            probe = await session.read_range(
                watched.remote_path, 0, min(file_size, self._cfg.header_probe_bytes)
            )
            header_data, _ = solver.parse_header(watched.file_path, watched.parser_kind, probe)
            cursor = FileCursor(
                run_id=cursor.run_id,
                file_path=cursor.file_path,
                parser_kind=cursor.parser_kind,
                file_size=file_size,
                mtime=mtime,
                head_prefix=head_prefix,
                forward_offset=cursor.forward_offset,
                backfill_start=cursor.backfill_start,
                backfill_end=cursor.backfill_end,
                header_data=header_data,
                parser_state=cursor.parser_state,
                status=cursor.status,
            )
        return await self._read_forward_range(
            session, store, solver, cursor, watched, file_size, mtime, head_prefix
        )

    async def _read_forward_range(
        self,
        session: RemoteSession,
        store: TimeSeriesStore,
        solver: Solver,
        cursor: FileCursor,
        watched: WatchedFile,
        file_size: int,
        mtime: float,
        head_prefix: bytes,
    ) -> SyncFileResult:
        offset = cursor.forward_offset
        fwd_state = dict(cursor.parser_state.get("forward", cursor.parser_state))
        if "forward" in cursor.parser_state:
            fwd_state = dict(cursor.parser_state["forward"])
        else:
            fwd_state = {k: v for k, v in cursor.parser_state.items() if k != "backfill"}

        header_data = cursor.header_data
        bytes_read = 0
        points_ingested = 0
        status_updates: list[SolverStatusUpdate] = []
        events: list[tuple[str, dict[str, Any]]] = []

        while offset < file_size:
            raw = await session.read_range(watched.remote_path, offset, self._cfg.chunk_bytes)
            if not raw:
                break
            complete, consumed = _split_complete_lines(raw)
            if consumed == 0:
                # Line longer than one chunk or incomplete trailing line at EOF.
                break
            parsed = solver.parse_chunk(
                watched.file_path,
                watched.parser_kind,
                _decode(complete),
                header_data=header_data,
                parser_state=fwd_state,
                is_backfill=False,
            )
            offset += consumed
            bytes_read += consumed
            fwd_state = dict(parsed.parser_state)
            if parsed.header_data is not None:
                header_data = parsed.header_data
            points_ingested += await self._persist_parsed(store, cursor.run_id, parsed)
            if parsed.status_update is not None:
                status_updates.append(parsed.status_update)
            events.extend(parsed.events)

            if len(raw) < self._cfg.chunk_bytes:
                break

        latest_cursor = await store.get_cursor(cursor.run_id, watched.file_path)
        bf_start = latest_cursor.backfill_start if latest_cursor else cursor.backfill_start
        bf_end = latest_cursor.backfill_end if latest_cursor else cursor.backfill_end
        bf_state = (
            dict(latest_cursor.parser_state.get("backfill", {}))
            if latest_cursor
            else dict(cursor.parser_state.get("backfill", {}))
        )
        new_status = FileStatus.BACKFILLING if bf_start < bf_end else FileStatus.ACTIVE

        await store.upsert_cursor(
            FileCursor(
                run_id=cursor.run_id,
                file_path=watched.file_path,
                parser_kind=watched.parser_kind,
                file_size=file_size,
                mtime=mtime,
                head_prefix=head_prefix,
                forward_offset=offset,
                backfill_start=bf_start,
                backfill_end=bf_end,
                header_data=header_data,
                parser_state={"forward": fwd_state, "backfill": bf_state},
                status=new_status,
            )
        )

        return SyncFileResult(
            file_path=watched.file_path,
            bytes_read=bytes_read,
            points_ingested=points_ingested,
            needs_backfill=bf_start < bf_end,
            status_updates=status_updates,
            events=events,
        )

    async def backfill_step(
        self,
        session: RemoteSession,
        store: TimeSeriesStore,
        solver: Solver,
        run_id: int,
        watched: WatchedFile,
        *,
        max_chunks: int | None = 1,
        on_progress: Callable[[BackfillProgress], Any] | None = None,
    ) -> BackfillProgress:
        """Read up to `max_chunks` (or all if None) of the `[backfill_start, backfill_end)` gap."""
        chunks_done = 0
        total_bytes = 0
        total_points = 0
        total_warnings = 0
        events: list[tuple[str, dict[str, Any]]] = []

        while max_chunks is None or chunks_done < max_chunks:
            async with self._lock_for(run_id, watched.file_path):
                cursor = await store.get_cursor(run_id, watched.file_path)
                if cursor is None or not cursor.needs_backfill:
                    return BackfillProgress(
                        file_path=watched.file_path,
                        bytes_read=total_bytes,
                        points_ingested=total_points,
                        remaining_bytes=0,
                        total_gap_bytes=0,
                        done=True,
                        events=events,
                        warning_delta=total_warnings,
                    )

                gap = cursor.backfill_end - cursor.backfill_start
                to_read = min(self._cfg.chunk_bytes, gap)
                raw = await session.read_range(watched.remote_path, cursor.backfill_start, to_read)
                if not raw:
                    return BackfillProgress(
                        file_path=watched.file_path,
                        bytes_read=total_bytes,
                        points_ingested=total_points,
                        remaining_bytes=gap,
                        total_gap_bytes=gap,
                        done=False,
                        events=events,
                        warning_delta=total_warnings,
                    )

                if cursor.backfill_start + len(raw) >= cursor.backfill_end:
                    complete = raw
                    consumed = len(raw)
                else:
                    complete, consumed = _split_complete_lines(raw)
                    if consumed == 0:
                        complete = raw
                        consumed = len(raw)

                bf_state = dict(cursor.parser_state.get("backfill", {}))
                parsed = solver.parse_chunk(
                    watched.file_path,
                    watched.parser_kind,
                    _decode(complete),
                    header_data=cursor.header_data,
                    parser_state=bf_state,
                    is_backfill=True,
                )
                pts = await self._persist_parsed(store, run_id, parsed)
                new_bf_start = cursor.backfill_start + consumed
                done = new_bf_start >= cursor.backfill_end
                fwd_state = dict(cursor.parser_state.get("forward", {}))

                await store.upsert_cursor(
                    FileCursor(
                        run_id=cursor.run_id,
                        file_path=cursor.file_path,
                        parser_kind=cursor.parser_kind,
                        file_size=cursor.file_size,
                        mtime=cursor.mtime,
                        head_prefix=cursor.head_prefix,
                        forward_offset=cursor.forward_offset,
                        backfill_start=new_bf_start,
                        backfill_end=cursor.backfill_end,
                        header_data=cursor.header_data,
                        parser_state={
                            "forward": fwd_state,
                            "backfill": dict(parsed.parser_state),
                        },
                        status=FileStatus.ACTIVE if done else FileStatus.BACKFILLING,
                    )
                )

                total_bytes += consumed
                total_points += pts
                if parsed.status_update is not None:
                    total_warnings += parsed.status_update.warning_delta
                events.extend(parsed.events)
                chunks_done += 1

                remaining = max(0, cursor.backfill_end - new_bf_start)
                progress = BackfillProgress(
                    file_path=watched.file_path,
                    bytes_read=total_bytes,
                    points_ingested=total_points,
                    remaining_bytes=remaining,
                    total_gap_bytes=cursor.backfill_end,
                    done=done,
                    events=list(events),
                    warning_delta=total_warnings,
                )
            if on_progress is not None:
                res = on_progress(progress)
                if asyncio.iscoroutine(res):
                    await res
            if done:
                return progress
            await asyncio.sleep(0)

        return progress

    async def _persist_parsed(
        self, store: TimeSeriesStore, run_id: int, parsed: ParsedChunk
    ) -> int:
        if not parsed.series:
            return 0
        await store.append_samples(run_id, parsed.series)
        return sum(len(xs) for xs, _ in parsed.series.values())
