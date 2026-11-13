"""SQLite implementation of `simmanager.contracts.timeseries.TimeSeriesStore`."""

from __future__ import annotations

import asyncio
import json
import math
import sys
from array import array
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import aiosqlite

from simmanager.contracts import storage as storage_contract
from simmanager.contracts import timeseries as contract
from simmanager.contracts.timeseries import (
    FileCursor,
    FileStatus,
    RunSolverState,
    SeriesKey,
    SeriesMeta,
    SeriesPoints,
    SolverState,
)
from simmanager.kernel import Context, Plugin

from .plugin import SqliteStorage

CHUNK_SIZE = 1000
LEVEL_FACTORS: tuple[tuple[int, int], ...] = ((1, 16), (2, 256))


def _pack_f64(values: Sequence[float]) -> bytes:
    arr = array("d", values)
    if sys.byteorder != "little":
        arr.byteswap()
    return arr.tobytes()


def _unpack_f64(blob: bytes) -> list[float]:
    arr = array("d")
    arr.frombytes(blob)
    if sys.byteorder != "little":
        arr.byteswap()
    return list(arr)


def _finite_minmax(values: Sequence[float]) -> tuple[float | None, float | None]:
    lo: float | None = None
    hi: float | None = None
    for v in values:
        if math.isfinite(v):
            if lo is None or v < lo:
                lo = v
            if hi is None or v > hi:
                hi = v
    return lo, hi


def _downsample_minmax(
    xs: Sequence[float], ys: Sequence[float], bucket_size: int
) -> tuple[list[float], list[float]]:
    """Reduce (xs, ys) to min/max pairs per bucket while preserving chronological order."""
    out_x: list[float] = []
    out_y: list[float] = []
    n = len(xs)
    for start in range(0, n, bucket_size):
        end = min(start + bucket_size, n)
        min_idx: int | None = None
        max_idx: int | None = None
        min_val = math.inf
        max_val = -math.inf
        for i in range(start, end):
            v = ys[i]
            if not math.isfinite(v):
                continue
            if v < min_val:
                min_val = v
                min_idx = i
            if v > max_val:
                max_val = v
                max_idx = i
        if min_idx is None or max_idx is None:
            out_x.append(xs[start])
            out_y.append(ys[start])
        elif min_idx == max_idx:
            out_x.append(xs[min_idx])
            out_y.append(ys[min_idx])
        elif min_idx < max_idx:
            out_x.extend((xs[min_idx], xs[max_idx]))
            out_y.extend((ys[min_idx], ys[max_idx]))
        else:
            out_x.extend((xs[max_idx], xs[min_idx]))
            out_y.extend((ys[max_idx], ys[min_idx]))
    return out_x, out_y


def _iter_chunks(
    xs: Sequence[float], ys: Sequence[float], chunk_size: int = CHUNK_SIZE
) -> list[tuple[Sequence[float], Sequence[float]]]:
    return [(xs[i : i + chunk_size], ys[i : i + chunk_size]) for i in range(0, len(xs), chunk_size)]


class SqliteTimeSeriesStore:
    """Implements `simmanager.contracts.timeseries.TimeSeriesStore` on an open SQLite db."""

    def __init__(self, db: aiosqlite.Connection, write_lock: asyncio.Lock) -> None:
        self._db = db
        self._write_lock = write_lock

    async def get_cursor(self, run_id: int, file_path: str) -> FileCursor | None:
        async with self._db.execute(
            "SELECT * FROM file_cursors WHERE run_id = ? AND file_path = ?",
            (run_id, file_path),
        ) as cur:
            row = await cur.fetchone()
            return None if row is None else _cursor_from_row(row)

    async def list_cursors(self, run_id: int) -> list[FileCursor]:
        async with self._db.execute(
            "SELECT * FROM file_cursors WHERE run_id = ? ORDER BY file_path", (run_id,)
        ) as cur:
            return [_cursor_from_row(row) async for row in cur]

    async def upsert_cursor(self, cursor: FileCursor) -> None:
        now = datetime.now(UTC).isoformat()
        header_json = json.dumps(cursor.header_data, ensure_ascii=False, default=str)
        state_json = json.dumps(cursor.parser_state, ensure_ascii=False, default=str)
        async with self._write_lock:
            await self._db.execute(
                """
                INSERT INTO file_cursors (
                    run_id, file_path, parser_kind, file_size, mtime, head_prefix,
                    forward_offset, backfill_start, backfill_end,
                    header_json, parser_state_json, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, file_path) DO UPDATE SET
                    parser_kind = excluded.parser_kind,
                    file_size = excluded.file_size,
                    mtime = excluded.mtime,
                    head_prefix = excluded.head_prefix,
                    forward_offset = excluded.forward_offset,
                    backfill_start = excluded.backfill_start,
                    backfill_end = excluded.backfill_end,
                    header_json = excluded.header_json,
                    parser_state_json = excluded.parser_state_json,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    cursor.run_id,
                    cursor.file_path,
                    cursor.parser_kind,
                    cursor.file_size,
                    cursor.mtime,
                    cursor.head_prefix,
                    cursor.forward_offset,
                    cursor.backfill_start,
                    cursor.backfill_end,
                    header_json,
                    state_json,
                    str(cursor.status),
                    now,
                ),
            )
            await self._db.commit()

    async def reset_file_data(self, run_id: int, file_path: str) -> None:
        async with self._write_lock:
            await self._db.execute(
                "DELETE FROM series WHERE run_id = ? AND file_path = ?",
                (run_id, file_path),
            )
            await self._db.commit()

    async def append_samples(
        self,
        run_id: int,
        batch: Mapping[SeriesKey, tuple[Sequence[float], Sequence[float]]],
    ) -> list[SeriesMeta]:
        if not batch:
            return []

        updated: list[SeriesMeta] = []
        async with self._write_lock:
            for key, (raw_xs, raw_ys) in batch.items():
                if len(raw_xs) != len(raw_ys):
                    raise ValueError(
                        f"series {key.name!r}: x/y length mismatch ({len(raw_xs)} != {len(raw_ys)})"
                    )
                if not raw_xs:
                    continue

                # Filter out non-finite x values so chunk bounds in SQLite stay valid.
                pairs = [
                    (float(x), float(y))
                    for x, y in zip(raw_xs, raw_ys, strict=True)
                    if math.isfinite(x)
                ]
                if not pairs:
                    continue
                xs = [p[0] for p in pairs]
                ys = [p[1] for p in pairs]

                series_id, existing = await self._ensure_series(run_id, key)
                await self._insert_level_chunks(series_id, 0, xs, ys)
                for level, bucket in LEVEL_FACTORS:
                    dx, dy = _downsample_minmax(xs, ys, bucket)
                    await self._insert_level_chunks(series_id, level, dx, dy)

                batch_min, batch_max = _finite_minmax(ys)
                new_count = existing.point_count + len(xs)
                new_last_x = existing.last_x
                new_last_y = existing.last_y
                if new_last_x is None or xs[-1] >= new_last_x:
                    new_last_x = xs[-1]
                    new_last_y = ys[-1] if math.isfinite(ys[-1]) else None

                new_min = _merge_min(existing.min_y, batch_min)
                new_max = _merge_max(existing.max_y, batch_max)

                await self._db.execute(
                    """
                    UPDATE series
                    SET point_count = ?, last_x = ?, last_y = ?, min_y = ?, max_y = ?,
                        unit = ?, x_name = ?, x_unit = ?
                    WHERE id = ?
                    """,
                    (
                        new_count,
                        new_last_x,
                        new_last_y,
                        new_min,
                        new_max,
                        key.unit,
                        key.x_name,
                        key.x_unit,
                        series_id,
                    ),
                )
                updated.append(
                    SeriesMeta(
                        id=series_id,
                        run_id=run_id,
                        file_path=key.file_path,
                        name=key.name,
                        qualifier=key.qualifier,
                        unit=key.unit,
                        x_name=key.x_name,
                        x_unit=key.x_unit,
                        point_count=new_count,
                        last_x=new_last_x,
                        last_y=new_last_y,
                        min_y=new_min,
                        max_y=new_max,
                    )
                )
            await self._db.commit()
        return updated

    async def _ensure_series(self, run_id: int, key: SeriesKey) -> tuple[int, SeriesMeta]:
        async with self._db.execute(
            "SELECT * FROM series"
            " WHERE run_id = ? AND file_path = ? AND name = ? AND qualifier = ?",
            (run_id, key.file_path, key.name, key.qualifier),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            return int(row["id"]), _series_from_row(row)

        cur = await self._db.execute(
            """
            INSERT INTO series (run_id, file_path, name, qualifier, unit, x_name, x_unit)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                key.file_path,
                key.name,
                key.qualifier,
                key.unit,
                key.x_name,
                key.x_unit,
            ),
        )
        assert cur.lastrowid is not None
        series_id = int(cur.lastrowid)
        return series_id, SeriesMeta(
            id=series_id,
            run_id=run_id,
            file_path=key.file_path,
            name=key.name,
            qualifier=key.qualifier,
            unit=key.unit,
            x_name=key.x_name,
            x_unit=key.x_unit,
            point_count=0,
            last_x=None,
            last_y=None,
            min_y=None,
            max_y=None,
        )

    async def _insert_level_chunks(
        self, series_id: int, level: int, xs: Sequence[float], ys: Sequence[float]
    ) -> None:
        rows = []
        for cx, cy in _iter_chunks(xs, ys):
            y_lo, y_hi = _finite_minmax(cy)
            rows.append(
                (
                    series_id,
                    level,
                    len(cx),
                    min(cx),
                    max(cx),
                    y_lo,
                    y_hi,
                    _pack_f64(cx),
                    _pack_f64(cy),
                )
            )
        await self._db.executemany(
            """
            INSERT INTO sample_chunks (
                series_id, level, count, x_min, x_max, y_min, y_max, x_blob, y_blob
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    async def list_series(self, run_id: int) -> list[SeriesMeta]:
        async with self._db.execute(
            "SELECT * FROM series WHERE run_id = ? ORDER BY file_path, name, qualifier",
            (run_id,),
        ) as cur:
            return [_series_from_row(row) async for row in cur]

    async def query_series(
        self,
        series_id: int,
        *,
        x_min: float | None = None,
        x_max: float | None = None,
        max_points: int = 2000,
    ) -> SeriesPoints:
        async with self._db.execute(
            "SELECT point_count FROM series WHERE id = ?", (series_id,)
        ) as cur:
            srow = await cur.fetchone()
        if srow is None:
            raise KeyError(series_id)
        total_points = int(srow["point_count"])
        if total_points == 0:
            return SeriesPoints(series_id=series_id, x=[], y=[], level=0, total_points=0)

        where = ["series_id = ?", "level = ?"]
        base_args: list[Any] = [series_id]
        if x_min is not None:
            where.append("x_max >= ?")
            base_args.append(x_min)
        if x_max is not None:
            where.append("x_min <= ?")
            base_args.append(x_max)
        where_sql = " AND ".join(where)

        chosen_level = 0
        for lvl in (0, 1, 2):
            args = [base_args[0], lvl, *base_args[1:]]
            async with self._db.execute(
                f"SELECT COALESCE(SUM(count), 0) AS n FROM sample_chunks WHERE {where_sql}",
                args,
            ) as cur:
                row = await cur.fetchone()
                cnt = int(row["n"]) if row is not None else 0
            chosen_level = lvl
            if cnt <= max_points * 2:
                break

        args = [base_args[0], chosen_level, *base_args[1:]]
        xs: list[float] = []
        ys: list[float] = []
        prev_x_max: float | None = None
        needs_sort = False
        async with self._db.execute(
            f"SELECT x_min, x_max, x_blob, y_blob FROM sample_chunks WHERE {where_sql}"
            " ORDER BY x_min ASC, id ASC",
            args,
        ) as cur:
            async for row in cur:
                cx = _unpack_f64(row["x_blob"])
                cy = _unpack_f64(row["y_blob"])
                c_min = float(row["x_min"])
                c_max = float(row["x_max"])
                if prev_x_max is not None and c_min < prev_x_max:
                    needs_sort = True
                prev_x_max = c_max if prev_x_max is None else max(prev_x_max, c_max)
                if x_min is not None or x_max is not None:
                    for x, y in zip(cx, cy, strict=True):
                        if (x_min is None or x >= x_min) and (x_max is None or x <= x_max):
                            xs.append(x)
                            ys.append(y)
                else:
                    xs.extend(cx)
                    ys.extend(cy)

        if needs_sort and xs:
            paired = sorted(zip(xs, ys, strict=True), key=lambda p: p[0])
            xs = [p[0] for p in paired]
            ys = [p[1] for p in paired]

        if max_points > 0 and len(xs) > max_points:
            bucket = max(2, math.ceil(len(xs) / max(1, max_points // 2)))
            xs, ys = _downsample_minmax(xs, ys, bucket)

        return SeriesPoints(
            series_id=series_id,
            x=xs,
            y=ys,
            level=chosen_level,
            total_points=total_points,
        )

    async def get_run_solver_state(self, run_id: int) -> RunSolverState | None:
        async with self._db.execute(
            "SELECT * FROM run_solver_state WHERE run_id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
            return None if row is None else _run_state_from_row(row)

    async def upsert_run_solver_state(self, state: RunSolverState) -> None:
        now = datetime.now(UTC).isoformat()
        meta_json = json.dumps(state.metadata, ensure_ascii=False, default=str)
        async with self._write_lock:
            await self._db.execute(
                """
                INSERT INTO run_solver_state (
                    run_id, solver, work_dir, case_dir, output_dir, solver_state,
                    progress, current_time, start_time, end_time, ncyc,
                    warning_count, error_message, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id) DO UPDATE SET
                    solver = excluded.solver,
                    work_dir = excluded.work_dir,
                    case_dir = COALESCE(excluded.case_dir, run_solver_state.case_dir),
                    output_dir = COALESCE(excluded.output_dir, run_solver_state.output_dir),
                    solver_state = excluded.solver_state,
                    progress = excluded.progress,
                    current_time = excluded.current_time,
                    start_time = COALESCE(excluded.start_time, run_solver_state.start_time),
                    end_time = COALESCE(excluded.end_time, run_solver_state.end_time),
                    ncyc = COALESCE(excluded.ncyc, run_solver_state.ncyc),
                    warning_count = excluded.warning_count,
                    error_message = COALESCE(
                        excluded.error_message, run_solver_state.error_message
                    ),
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.run_id,
                    state.solver,
                    state.work_dir,
                    state.case_dir,
                    state.output_dir,
                    str(state.solver_state),
                    state.progress,
                    state.current_time,
                    state.start_time,
                    state.end_time,
                    state.ncyc,
                    state.warning_count,
                    state.error_message,
                    meta_json,
                    now,
                ),
            )
            await self._db.commit()


def _merge_min(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _merge_max(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _cursor_from_row(row: aiosqlite.Row) -> FileCursor:
    return FileCursor(
        run_id=int(row["run_id"]),
        file_path=str(row["file_path"]),
        parser_kind=str(row["parser_kind"]),
        file_size=int(row["file_size"]),
        mtime=float(row["mtime"]),
        head_prefix=bytes(row["head_prefix"]),
        forward_offset=int(row["forward_offset"]),
        backfill_start=int(row["backfill_start"]),
        backfill_end=int(row["backfill_end"]),
        header_data=json.loads(row["header_json"]),
        parser_state=json.loads(row["parser_state_json"]),
        status=FileStatus(row["status"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _series_from_row(row: aiosqlite.Row) -> SeriesMeta:
    return SeriesMeta(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        file_path=str(row["file_path"]),
        name=str(row["name"]),
        qualifier=str(row["qualifier"]),
        unit=str(row["unit"]),
        x_name=str(row["x_name"]),
        x_unit=str(row["x_unit"]),
        point_count=int(row["point_count"]),
        last_x=row["last_x"],
        last_y=row["last_y"],
        min_y=row["min_y"],
        max_y=row["max_y"],
    )


def _run_state_from_row(row: aiosqlite.Row) -> RunSolverState:
    return RunSolverState(
        run_id=int(row["run_id"]),
        solver=str(row["solver"]),
        work_dir=str(row["work_dir"]),
        case_dir=row["case_dir"],
        output_dir=row["output_dir"],
        solver_state=SolverState(row["solver_state"]),
        progress=row["progress"],
        current_time=row["current_time"],
        start_time=row["start_time"],
        end_time=row["end_time"],
        ncyc=row["ncyc"],
        warning_count=int(row["warning_count"]),
        error_message=row["error_message"],
        metadata=json.loads(row["metadata_json"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class SqliteTimeSeriesPlugin(Plugin):
    name = "timeseries-sqlite"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        storage_contract.SERVICE_KEY: storage_contract.CONTRACT_VERSION
    }
    description = "Durable time-series chunks and file cursors on the SQLite storage connection."

    def apply(self, ctx: Context) -> SqliteTimeSeriesStore:
        storage = ctx.services.require(storage_contract.SERVICE_KEY)
        if not isinstance(storage, SqliteStorage):
            raise TypeError(
                f"{self.name} requires storage-sqlite's SqliteStorage, got {type(storage).__name__}"
            )
        return SqliteTimeSeriesStore(storage.connection, storage.write_lock)
