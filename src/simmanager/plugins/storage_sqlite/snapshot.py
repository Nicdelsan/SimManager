"""SQLite implementation of `simmanager.contracts.snapshot.SnapshotStore`."""

from __future__ import annotations

import asyncio
import hashlib
import json
import zlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import aiosqlite

from simmanager.contracts import snapshot as contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts.snapshot import (
    ContentStatus,
    RunNoteRecord,
    SnapshotBlob,
    SnapshotFileRecord,
    SnapshotFileSpec,
    SnapshotRecord,
    SnapshotStatus,
    SnapshotTrigger,
)
from simmanager.kernel import Context, Plugin

from .plugin import SqliteStorage


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _compute_manifest_sha256(rows: Sequence[tuple[str, str | None, int, str]]) -> str:
    h = hashlib.sha256()
    for rel_path, sha, size_bytes, status in sorted(rows, key=lambda r: r[0]):
        h.update(f"{rel_path}\0{sha or ''}\0{size_bytes}\0{status}\n".encode())
    return h.hexdigest()


class SqliteSnapshotStore:
    """Implements `simmanager.contracts.snapshot.SnapshotStore` on the shared SQLite db."""

    def __init__(self, db: aiosqlite.Connection, write_lock: asyncio.Lock) -> None:
        self._db = db
        self._write_lock = write_lock

    async def save_snapshot(
        self,
        *,
        run_id: int,
        trigger: SnapshotTrigger,
        effective: bool,
        status: SnapshotStatus,
        late: bool,
        late_files: Sequence[str],
        solver: str | None,
        work_dir: str,
        manifest_sha256: str,
        error_message: str | None,
        files: Sequence[SnapshotFileSpec],
        blobs: Mapping[str, tuple[bytes, bool, str]],
    ) -> SnapshotRecord:
        now = _now_iso()
        total_bytes = sum(f.size_bytes for f in files)
        late_json = json.dumps(list(late_files), ensure_ascii=False)
        async with self._write_lock:
            try:
                for sha, (raw_bytes, is_text, encoding) in blobs.items():
                    compressed = zlib.compress(raw_bytes, level=6)
                    await self._db.execute(
                        """
                        INSERT OR IGNORE INTO snapshot_blobs (
                            sha256, size_bytes, compressed_bytes, is_text, encoding, data,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sha,
                            len(raw_bytes),
                            len(compressed),
                            int(is_text),
                            encoding,
                            compressed,
                            now,
                        ),
                    )
                if effective:
                    await self._db.execute(
                        "UPDATE snapshots SET effective = 0 WHERE run_id = ? AND effective = 1",
                        (run_id,),
                    )
                cur = await self._db.execute(
                    """
                    INSERT INTO snapshots (
                        run_id, trigger, effective, status, late, late_files_json,
                        solver, work_dir, manifest_sha256, file_count, total_bytes,
                        error_message, captured_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(trigger),
                        int(effective),
                        str(status),
                        int(late),
                        late_json,
                        solver,
                        work_dir,
                        manifest_sha256,
                        len(files),
                        total_bytes,
                        error_message,
                        now,
                    ),
                )
                assert cur.lastrowid is not None
                snapshot_id = int(cur.lastrowid)
                if files:
                    await self._db.executemany(
                        """
                        INSERT INTO snapshot_files (
                            snapshot_id, rel_path, size_bytes, mtime, sha256, content_status
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                snapshot_id,
                                f.rel_path,
                                f.size_bytes,
                                f.mtime,
                                f.sha256,
                                str(f.content_status),
                            )
                            for f in files
                        ],
                    )
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise
        loaded = await self.get_snapshot(snapshot_id, include_files=True)
        assert loaded is not None
        return loaded

    async def get_snapshot(
        self, snapshot_id: int, *, include_files: bool = True
    ) -> SnapshotRecord | None:
        async with self._db.execute(
            "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        files = await self._load_files(snapshot_id) if include_files else ()
        return _snapshot_from_row(row, files)

    async def list_snapshots(
        self, run_id: int, *, include_files: bool = False
    ) -> list[SnapshotRecord]:
        async with self._db.execute(
            "SELECT * FROM snapshots WHERE run_id = ? ORDER BY captured_at ASC, id ASC",
            (run_id,),
        ) as cur:
            rows = await cur.fetchall()
        out: list[SnapshotRecord] = []
        for row in rows:
            sid = int(row["id"])
            files = await self._load_files(sid) if include_files else ()
            out.append(_snapshot_from_row(row, files))
        return out

    async def effective_snapshot(
        self, run_id: int, *, include_files: bool = True
    ) -> SnapshotRecord | None:
        async with self._db.execute(
            """
            SELECT * FROM snapshots
            WHERE run_id = ?
            ORDER BY effective DESC, captured_at DESC, id DESC
            LIMIT 1
            """,
            (run_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        sid = int(row["id"])
        files = await self._load_files(sid) if include_files else ()
        return _snapshot_from_row(row, files)

    async def find_reusable_sha256(
        self, work_dir: str, rel_path: str, size_bytes: int, mtime: float
    ) -> str | None:
        async with self._db.execute(
            """
            SELECT sf.sha256
            FROM snapshot_files sf
            JOIN snapshots s ON s.id = sf.snapshot_id
            WHERE s.work_dir = ?
              AND sf.rel_path = ?
              AND sf.size_bytes = ?
              AND ABS(sf.mtime - ?) < 1e-6
              AND sf.sha256 IS NOT NULL
            ORDER BY sf.id DESC
            LIMIT 1
            """,
            (work_dir, rel_path, size_bytes, mtime),
        ) as cur:
            row = await cur.fetchone()
        return str(row["sha256"]) if row is not None and row["sha256"] else None

    async def update_file_hash(
        self,
        snapshot_id: int,
        rel_path: str,
        sha256: str,
        *,
        content_status: ContentStatus = ContentStatus.OVERSIZE_HASHED,
    ) -> bool:
        async with self._write_lock:
            try:
                cur = await self._db.execute(
                    """
                    UPDATE snapshot_files
                    SET sha256 = ?, content_status = ?
                    WHERE snapshot_id = ? AND rel_path = ?
                    """,
                    (sha256, str(content_status), snapshot_id, rel_path),
                )
                if cur.rowcount == 0:
                    await self._db.rollback()
                    return False
                async with self._db.execute(
                    "SELECT rel_path, sha256, size_bytes, content_status"
                    " FROM snapshot_files WHERE snapshot_id = ?",
                    (snapshot_id,),
                ) as fcur:
                    frows = [
                        (
                            str(r["rel_path"]),
                            r["sha256"],
                            int(r["size_bytes"]),
                            str(r["content_status"]),
                        )
                        async for r in fcur
                    ]
                new_manifest = _compute_manifest_sha256(frows)
                await self._db.execute(
                    "UPDATE snapshots SET manifest_sha256 = ? WHERE id = ?",
                    (new_manifest, snapshot_id),
                )
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise
        return True

    async def get_blob(self, sha256: str) -> SnapshotBlob | None:
        async with self._db.execute(
            "SELECT * FROM snapshot_blobs WHERE sha256 = ?", (sha256,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return SnapshotBlob(
            sha256=str(row["sha256"]),
            size_bytes=int(row["size_bytes"]),
            is_text=bool(row["is_text"]),
            encoding=str(row["encoding"]),
            data=zlib.decompress(bytes(row["data"])),
        )

    async def get_note(self, run_id: int) -> RunNoteRecord | None:
        async with self._db.execute(
            "SELECT * FROM run_notes WHERE run_id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return RunNoteRecord(
            run_id=int(row["run_id"]),
            note=str(row["note"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def set_note(self, run_id: int, note: str) -> RunNoteRecord:
        now = _now_iso()
        async with self._write_lock:
            await self._db.execute(
                """
                INSERT INTO run_notes (run_id, note, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (run_id, note, now),
            )
            await self._db.commit()
        return RunNoteRecord(run_id=run_id, note=note, updated_at=datetime.fromisoformat(now))

    async def _load_files(self, snapshot_id: int) -> tuple[SnapshotFileRecord, ...]:
        async with self._db.execute(
            "SELECT * FROM snapshot_files WHERE snapshot_id = ? ORDER BY rel_path ASC",
            (snapshot_id,),
        ) as cur:
            return tuple([_file_from_row(row) async for row in cur])


def _snapshot_from_row(
    row: aiosqlite.Row, files: tuple[SnapshotFileRecord, ...]
) -> SnapshotRecord:
    raw_late: Any = json.loads(row["late_files_json"] or "[]")
    late_files = tuple(str(x) for x in raw_late) if isinstance(raw_late, list) else ()
    return SnapshotRecord(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        trigger=SnapshotTrigger(row["trigger"]),
        effective=bool(row["effective"]),
        status=SnapshotStatus(row["status"]),
        late=bool(row["late"]),
        late_files=late_files,
        solver=row["solver"],
        work_dir=str(row["work_dir"]),
        manifest_sha256=str(row["manifest_sha256"]),
        file_count=int(row["file_count"]),
        total_bytes=int(row["total_bytes"]),
        error_message=row["error_message"],
        captured_at=datetime.fromisoformat(row["captured_at"]),
        files=files,
    )


def _file_from_row(row: aiosqlite.Row) -> SnapshotFileRecord:
    return SnapshotFileRecord(
        id=int(row["id"]),
        snapshot_id=int(row["snapshot_id"]),
        rel_path=str(row["rel_path"]),
        size_bytes=int(row["size_bytes"]),
        mtime=float(row["mtime"]),
        sha256=row["sha256"],
        content_status=ContentStatus(row["content_status"]),
    )


class SqliteSnapshotPlugin(Plugin):
    name = "snapshot-sqlite"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        storage_contract.SERVICE_KEY: storage_contract.CONTRACT_VERSION
    }
    description = "Content-addressed input snapshot and run-note store on the SQLite connection."

    def apply(self, ctx: Context) -> SqliteSnapshotStore:
        storage = ctx.services.require(storage_contract.SERVICE_KEY)
        if not isinstance(storage, SqliteStorage):
            raise TypeError(
                f"{self.name} requires storage-sqlite's SqliteStorage, got {type(storage).__name__}"
            )
        return SqliteSnapshotStore(storage.connection, storage.write_lock)
