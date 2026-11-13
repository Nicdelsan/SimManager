"""`storage` service backed by a local SQLite database (WAL mode, single writer)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel, ConfigDict

from simmanager import paths
from simmanager.contracts import storage as contract
from simmanager.contracts.storage import EventRecord
from simmanager.kernel import Context, Plugin

from .migrate import apply_migrations, bundled_migrations


class SqliteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    """Database file. Default: <data dir>/simmanager.db. `:memory:` for tests."""


class SqliteStorage:
    """Implements `simmanager.contracts.storage.Storage`."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str) -> SqliteStorage:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=5000")
            await apply_migrations(db, bundled_migrations())
        except BaseException:
            await db.close()
            raise
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    # ------------------------------------------------------------ event log
    async def append_event(
        self,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        source: str,
        cluster_id: str | None = None,
        run_id: int | None = None,
    ) -> int:
        ts = datetime.now(UTC).isoformat()
        body = json.dumps(dict(payload or {}), default=str, ensure_ascii=False)
        async with self._write_lock:
            cur = await self._db.execute(
                "INSERT INTO events (ts, kind, source, cluster_id, run_id, payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts, kind, source, cluster_id, run_id, body),
            )
            await self._db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def events(
        self,
        *,
        since_id: int = 0,
        kinds: Sequence[str] | None = None,
        run_id: int | None = None,
        limit: int = 500,
    ) -> list[EventRecord]:
        sql = "SELECT * FROM events WHERE id > ?"
        args: list[Any] = [since_id]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        if run_id is not None:
            sql += " AND run_id = ?"
            args.append(run_id)
        sql += " ORDER BY id LIMIT ?"
        args.append(limit)
        async with self._db.execute(sql, args) as cur:
            return [_event(row) async for row in cur]

    async def latest_events(self, limit: int = 50) -> list[EventRecord]:
        async with self._db.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return [_event(row) async for row in cur]


def _event(row: aiosqlite.Row) -> EventRecord:
    return EventRecord(
        id=row["id"],
        ts=datetime.fromisoformat(row["ts"]),
        kind=row["kind"],
        source=row["source"],
        cluster_id=row["cluster_id"],
        run_id=row["run_id"],
        payload=json.loads(row["payload"]),
    )


class SqliteStoragePlugin(Plugin):
    name = "storage-sqlite"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    config_schema = SqliteConfig
    description = "Durable storage (event log, jobs, runs, samples) in a local SQLite file."

    async def apply(self, ctx: Context) -> SqliteStorage:
        assert isinstance(ctx.config, SqliteConfig)
        path = ctx.config.path or str(paths.default_database_file())
        storage = await SqliteStorage.open(path)
        # Closing the connection is the only teardown: data is never touched on unload.
        ctx.add_disposer(storage.close, "close database")
        ctx.log.info("database ready at %s", path)
        return storage
