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
from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.contracts.storage import EventRecord, JobRecord, JobUpsert, RunRecord
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

    @property
    def connection(self) -> aiosqlite.Connection:
        return self._db

    @property
    def write_lock(self) -> asyncio.Lock:
        return self._write_lock

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

    # ----------------------------------------------------------------- jobs/runs
    async def upsert_job(self, cluster_id: str, info: JobInfo, *, active: bool) -> JobUpsert:
        now = datetime.now(UTC).isoformat()
        values = _job_values(info)
        async with self._write_lock:
            try:
                previous = await self._job_row(cluster_id, info.job_id)
                if previous is None:
                    cur = await self._db.execute(
                        f"INSERT INTO jobs (cluster_id, job_id, {', '.join(values)},"
                        " active, first_seen, last_seen)"
                        f" VALUES (?, ?, {', '.join('?' * len(values))}, ?, ?, ?)",
                        (
                            cluster_id,
                            info.job_id,
                            *{**values, "name": info.name}.values(),
                            int(active),
                            now,
                            now,
                        ),
                    )
                    await self._db.execute(
                        "INSERT INTO runs (job_pk, origin, cluster_id, work_dir, created_at)"
                        " VALUES (?, 'discovered', ?, ?, ?)",
                        (cur.lastrowid, cluster_id, info.work_dir, now),
                    )
                else:
                    sets = ", ".join(
                        f"{col} = ?" if col in _ALWAYS_SET else f"{col} = COALESCE(?, {col})"
                        for col in values
                    )
                    await self._db.execute(
                        f"UPDATE jobs SET {sets}, active = ?, last_seen = ? WHERE id = ?",
                        (*values.values(), int(active), now, previous["id"]),
                    )
                row = await self._job_row(cluster_id, info.job_id)
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise
        assert row is not None
        return JobUpsert(
            record=_job_record(row),
            created=previous is None,
            previous=None if previous is None else _job_record(previous).info,
        )

    async def jobs(
        self,
        *,
        cluster_id: str | None = None,
        active: bool | None = None,
        limit: int = 500,
    ) -> list[JobRecord]:
        sql = _JOB_SELECT + " WHERE 1 = 1"
        args: list[Any] = []
        if cluster_id is not None:
            sql += " AND j.cluster_id = ?"
            args.append(cluster_id)
        if active is not None:
            sql += " AND j.active = ?"
            args.append(int(active))
        sql += " ORDER BY COALESCE(j.submit_time, j.first_seen) DESC, j.id DESC LIMIT ?"
        args.append(limit)
        async with self._db.execute(sql, args) as cur:
            return [_job_record(row) async for row in cur]

    async def create_run(
        self,
        *,
        work_dir: str | None = None,
        cluster_id: str | None = None,
    ) -> RunRecord:
        now = datetime.now(UTC).isoformat()
        async with self._write_lock:
            cur = await self._db.execute(
                "INSERT INTO runs (job_pk, origin, cluster_id, work_dir, created_at)"
                " VALUES (NULL, 'manual', ?, ?, ?)",
                (cluster_id, work_dir, now),
            )
            await self._db.commit()
        assert cur.lastrowid is not None
        return RunRecord(
            id=cur.lastrowid,
            job_pk=None,
            origin="manual",
            cluster_id=cluster_id,
            work_dir=work_dir,
            created_at=datetime.fromisoformat(now),
        )

    async def run(self, run_id: int) -> RunRecord | None:
        async with self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return RunRecord(
            id=row["id"],
            job_pk=row["job_pk"],
            origin=row["origin"],
            cluster_id=row["cluster_id"],
            work_dir=row["work_dir"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def _job_row(self, cluster_id: str, job_id: str) -> aiosqlite.Row | None:
        async with self._db.execute(
            _JOB_SELECT + " WHERE j.cluster_id = ? AND j.job_id = ?", (cluster_id, job_id)
        ) as cur:
            return await cur.fetchone()


_JOB_SELECT = "SELECT j.*, r.id AS run_id FROM jobs j LEFT JOIN runs r ON r.job_pk = j.id"
_ALWAYS_SET = {"state", "raw_state", "reason"}
_TIME_COLUMNS = ("submit_time", "start_time", "end_time")


def _job_values(info: JobInfo) -> dict[str, Any]:
    """Column -> value for the jobs table (None = unknown, keeps the stored value)."""
    values: dict[str, Any] = {
        "name": info.name or None,
        "user": info.user,
        "state": str(info.state),
        "raw_state": info.raw_state,
        "reason": info.reason,
        "work_dir": info.work_dir,
        "partition": info.partition,
        "nodes": info.nodes,
        "node_list": info.node_list,
        "exit_code": info.exit_code,
        "elapsed_s": info.elapsed_s,
        "time_limit_s": info.time_limit_s,
        "stdout_path": info.stdout_path,
    }
    for col in _TIME_COLUMNS:
        value = getattr(info, col)
        values[col] = value.isoformat() if value is not None else None
    return values


def _job_record(row: aiosqlite.Row) -> JobRecord:
    def ts(col: str) -> datetime | None:
        return datetime.fromisoformat(row[col]) if row[col] else None

    info = JobInfo(
        job_id=row["job_id"],
        name=row["name"] or "",
        state=JobState(row["state"]),
        raw_state=row["raw_state"],
        user=row["user"],
        work_dir=row["work_dir"],
        partition=row["partition"],
        nodes=row["nodes"],
        node_list=row["node_list"],
        reason=row["reason"],
        exit_code=row["exit_code"],
        elapsed_s=row["elapsed_s"],
        time_limit_s=row["time_limit_s"],
        submit_time=ts("submit_time"),
        start_time=ts("start_time"),
        end_time=ts("end_time"),
        stdout_path=row["stdout_path"],
    )
    return JobRecord(
        pk=row["id"],
        cluster_id=row["cluster_id"],
        run_id=row["run_id"],
        active=bool(row["active"]),
        first_seen=datetime.fromisoformat(row["first_seen"]),
        last_seen=datetime.fromisoformat(row["last_seen"]),
        info=info,
    )


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
