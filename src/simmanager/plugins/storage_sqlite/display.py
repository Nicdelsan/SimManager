"""SQLite implementation of `simmanager.contracts.display.DisplayStore` (migration 0006)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

import aiosqlite

from simmanager.contracts import display as contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts.display import (
    ExecutionRecord,
    ExecutionStatus,
    ExecutionTrigger,
    FoundImage,
    ImageRecord,
    ImageSkip,
    ScheduleRecord,
    ScriptDraft,
    ScriptRecord,
    ScriptVersion,
)
from simmanager.kernel import Context, Plugin

from .plugin import SqliteStorage


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def script_sha256(body: str, setup: str, interpreter: str) -> str:
    """Identity of a script version: what would run on the cluster."""
    h = hashlib.sha256()
    for part in (interpreter, setup, body):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


_EXECUTION_SELECT = """
    SELECT e.*, v.script_id, v.number AS version_number, s.name AS script_name
    FROM display_executions e
    JOIN display_script_versions v ON v.id = e.version_id
    JOIN display_scripts s ON s.id = v.script_id
"""


class SqliteDisplayStore:
    """Implements `DisplayStore` on the shared SQLite connection and write lock."""

    def __init__(self, db: aiosqlite.Connection, write_lock: asyncio.Lock) -> None:
        self._db = db
        self._write_lock = write_lock

    # ------------------------------------------------------------------ scripts
    async def save_script(self, draft: ScriptDraft) -> tuple[ScriptVersion, bool]:
        sha = script_sha256(draft.body, draft.setup, draft.interpreter)
        now = _now_iso()
        async with self._write_lock:
            try:
                script_id = await self._script_id(draft.name, now)
                latest = await self._latest_row(script_id)
                if latest is not None and latest["sha256"] == sha:
                    await self._db.commit()
                    return _version(latest), False
                number = 1 if latest is None else int(latest["number"]) + 1
                cur = await self._db.execute(
                    "INSERT INTO display_script_versions (script_id, number, sha256, body,"
                    " setup, interpreter, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        script_id,
                        number,
                        sha,
                        draft.body,
                        draft.setup,
                        draft.interpreter,
                        draft.message,
                        now,
                    ),
                )
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise
        assert cur.lastrowid is not None
        created = await self.version(cur.lastrowid)
        assert created is not None
        return created, True

    async def _script_id(self, name: str, now: str) -> int:
        """Caller holds the write lock and commits."""
        async with self._db.execute(
            "SELECT id FROM display_scripts WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            return int(row["id"])
        cur2 = await self._db.execute(
            "INSERT INTO display_scripts (name, created_at) VALUES (?, ?)", (name, now)
        )
        assert cur2.lastrowid is not None
        return cur2.lastrowid

    async def _latest_row(self, script_id: int) -> aiosqlite.Row | None:
        async with self._db.execute(
            "SELECT * FROM display_script_versions WHERE script_id = ?"
            " ORDER BY number DESC LIMIT 1",
            (script_id,),
        ) as cur:
            return await cur.fetchone()

    async def scripts(self) -> list[ScriptRecord]:
        async with self._db.execute("SELECT * FROM display_scripts ORDER BY name") as cur:
            rows = await cur.fetchall()
        out = []
        for row in rows:
            latest = await self._latest_row(int(row["id"]))
            if latest is not None:
                out.append(_script(row, latest))
        return out

    async def script(self, script_id: int) -> ScriptRecord | None:
        async with self._db.execute(
            "SELECT * FROM display_scripts WHERE id = ?", (script_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        latest = await self._latest_row(script_id)
        return _script(row, latest) if latest is not None else None

    async def versions(self, script_id: int) -> list[ScriptVersion]:
        async with self._db.execute(
            "SELECT * FROM display_script_versions WHERE script_id = ? ORDER BY number DESC",
            (script_id,),
        ) as cur:
            return [_version(row) async for row in cur]

    async def version(self, version_id: int) -> ScriptVersion | None:
        async with self._db.execute(
            "SELECT * FROM display_script_versions WHERE id = ?", (version_id,)
        ) as cur:
            row = await cur.fetchone()
        return _version(row) if row is not None else None

    # --------------------------------------------------------------- executions
    async def start_execution(
        self, run_id: int, version_id: int, trigger: ExecutionTrigger
    ) -> ExecutionRecord:
        async with self._write_lock:
            cur = await self._db.execute(
                "INSERT INTO display_executions (run_id, version_id, trigger, status,"
                " started_at) VALUES (?, ?, ?, 'running', ?)",
                (run_id, version_id, str(trigger), _now_iso()),
            )
            await self._db.commit()
        assert cur.lastrowid is not None
        record = await self.execution(cur.lastrowid)
        assert record is not None
        return record

    async def finish_execution(
        self,
        execution_id: int,
        *,
        status: ExecutionStatus,
        exit_status: int | None,
        stdout_tail: str,
        stderr_tail: str,
        error: str,
    ) -> None:
        async with self._write_lock:
            await self._db.execute(
                "UPDATE display_executions SET status = ?, finished_at = ?, exit_status = ?,"
                " stdout_tail = ?, stderr_tail = ?, error = ? WHERE id = ?",
                (
                    str(status),
                    _now_iso(),
                    exit_status,
                    stdout_tail,
                    stderr_tail,
                    error,
                    execution_id,
                ),
            )
            await self._db.commit()

    async def interrupt_running(self) -> int:
        async with self._write_lock:
            cur = await self._db.execute(
                "UPDATE display_executions SET status = 'interrupted', finished_at = ?"
                " WHERE status = 'running'",
                (_now_iso(),),
            )
            await self._db.commit()
        return cur.rowcount

    async def add_image(
        self,
        execution_id: int,
        found: FoundImage,
        media_type: str,
        data: bytes | None,
        skipped: ImageSkip | None,
        detail: str = "",
    ) -> ImageRecord:
        async with self._write_lock:
            try:
                sha = await self._put_blob(data) if data is not None else None
                cur = await self._db.execute(
                    "INSERT INTO display_images (execution_id, path, size_bytes, mtime,"
                    " media_type, sha256, skipped, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        execution_id,
                        found.path,
                        found.size,
                        found.mtime,
                        media_type,
                        sha,
                        str(skipped) if skipped else None,
                        detail,
                    ),
                )
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise
        assert cur.lastrowid is not None
        record = await self.image(cur.lastrowid)
        assert record is not None
        return record

    async def attach_image_data(self, image_id: int, data: bytes) -> ImageRecord:
        async with self._write_lock:
            try:
                sha = await self._put_blob(data)
                await self._db.execute(
                    "UPDATE display_images SET sha256 = ?, size_bytes = ?, skipped = NULL,"
                    " detail = '' WHERE id = ?",
                    (sha, len(data), image_id),
                )
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise
        record = await self.image(image_id)
        assert record is not None
        return record

    async def _put_blob(self, data: bytes) -> str:
        """Caller holds the write lock and commits."""
        sha = hashlib.sha256(data).hexdigest()
        await self._db.execute(
            "INSERT OR IGNORE INTO display_image_blobs (sha256, size_bytes, data) VALUES (?, ?, ?)",
            (sha, len(data), data),
        )
        return sha

    async def executions(self, run_id: int, *, limit: int = 50) -> list[ExecutionRecord]:
        async with self._db.execute(
            _EXECUTION_SELECT + " WHERE e.run_id = ? ORDER BY e.id DESC LIMIT ?",
            (run_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [await self._with_images(row) for row in rows]

    async def execution(self, execution_id: int) -> ExecutionRecord | None:
        async with self._db.execute(_EXECUTION_SELECT + " WHERE e.id = ?", (execution_id,)) as cur:
            row = await cur.fetchone()
        return await self._with_images(row) if row is not None else None

    async def _with_images(self, row: aiosqlite.Row) -> ExecutionRecord:
        async with self._db.execute(
            "SELECT * FROM display_images WHERE execution_id = ? ORDER BY mtime DESC, id",
            (row["id"],),
        ) as cur:
            images = tuple([_image(r) async for r in cur])
        return _execution(row, images)

    async def image(self, image_id: int) -> ImageRecord | None:
        async with self._db.execute(
            "SELECT * FROM display_images WHERE id = ?", (image_id,)
        ) as cur:
            row = await cur.fetchone()
        return _image(row) if row is not None else None

    async def image_data(self, image_id: int) -> bytes | None:
        async with self._db.execute(
            "SELECT b.data FROM display_images i JOIN display_image_blobs b"
            " ON b.sha256 = i.sha256 WHERE i.id = ?",
            (image_id,),
        ) as cur:
            row = await cur.fetchone()
        return bytes(row["data"]) if row is not None else None

    # ---------------------------------------------------------------- schedules
    async def set_schedule(
        self, run_id: int, script_id: int, *, interval_s: int, enabled: bool
    ) -> ScheduleRecord:
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO display_schedules (run_id, script_id, interval_s, enabled,"
                " approved_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, script_id) DO UPDATE SET interval_s = excluded.interval_s,"
                " enabled = excluded.enabled, approved_at = excluded.approved_at",
                (run_id, script_id, interval_s, int(enabled), _now_iso()),
            )
            await self._db.commit()
        found = [s for s in await self.schedules(run_id) if s.script_id == script_id]
        return found[0]

    async def schedules(self, run_id: int | None = None) -> list[ScheduleRecord]:
        sql = "SELECT * FROM display_schedules"
        args: list[Any] = []
        if run_id is not None:
            sql += " WHERE run_id = ?"
            args.append(run_id)
        sql += " ORDER BY run_id, script_id"
        async with self._db.execute(sql, args) as cur:
            return [_schedule(row) async for row in cur]

    async def mark_scheduled_run(self, run_id: int, script_id: int, at: datetime) -> None:
        async with self._write_lock:
            await self._db.execute(
                "UPDATE display_schedules SET last_run_at = ? WHERE run_id = ? AND script_id = ?",
                (at.isoformat(), run_id, script_id),
            )
            await self._db.commit()


# ------------------------------------------------------------------- row mapping
def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _version(row: aiosqlite.Row) -> ScriptVersion:
    return ScriptVersion(
        id=int(row["id"]),
        script_id=int(row["script_id"]),
        number=int(row["number"]),
        sha256=str(row["sha256"]),
        body=str(row["body"]),
        setup=str(row["setup"]),
        interpreter=str(row["interpreter"]),
        message=str(row["message"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _script(row: aiosqlite.Row, latest: aiosqlite.Row) -> ScriptRecord:
    return ScriptRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        latest=_version(latest),
    )


def _image(row: aiosqlite.Row) -> ImageRecord:
    return ImageRecord(
        id=int(row["id"]),
        execution_id=int(row["execution_id"]),
        path=str(row["path"]),
        size=int(row["size_bytes"]),
        mtime=float(row["mtime"]),
        media_type=str(row["media_type"]),
        sha256=row["sha256"],
        skipped=ImageSkip(row["skipped"]) if row["skipped"] else None,
        detail=str(row["detail"]),
    )


def _execution(row: aiosqlite.Row, images: tuple[ImageRecord, ...]) -> ExecutionRecord:
    return ExecutionRecord(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        script_id=int(row["script_id"]),
        script_name=str(row["script_name"]),
        version_id=int(row["version_id"]),
        version_number=int(row["version_number"]),
        trigger=ExecutionTrigger(row["trigger"]),
        status=ExecutionStatus(row["status"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=_dt(row["finished_at"]),
        exit_status=row["exit_status"],
        stdout_tail=str(row["stdout_tail"]),
        stderr_tail=str(row["stderr_tail"]),
        error=str(row["error"]),
        images=images,
    )


def _schedule(row: aiosqlite.Row) -> ScheduleRecord:
    return ScheduleRecord(
        run_id=int(row["run_id"]),
        script_id=int(row["script_id"]),
        interval_s=int(row["interval_s"]),
        enabled=bool(row["enabled"]),
        approved_at=datetime.fromisoformat(row["approved_at"]),
        last_run_at=_dt(row["last_run_at"]),
    )


class SqliteDisplayPlugin(Plugin):
    name = "display-sqlite"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        storage_contract.SERVICE_KEY: storage_contract.CONTRACT_VERSION
    }
    description = "Display script library, executions and image history on SQLite."

    def apply(self, ctx: Context) -> SqliteDisplayStore:
        storage = ctx.services.require(storage_contract.SERVICE_KEY)
        if not isinstance(storage, SqliteStorage):
            raise TypeError(
                f"{self.name} requires storage-sqlite's SqliteStorage, got {type(storage).__name__}"
            )
        return SqliteDisplayStore(storage.connection, storage.write_lock)
