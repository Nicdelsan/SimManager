from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pytest

from simmanager.contracts.timeseries import (
    FileCursor,
    FileStatus,
    RunSolverState,
    SeriesKey,
    SolverState,
    TimeSeriesStore,
)
from simmanager.kernel import Kernel, PluginState
from simmanager.plugins.storage_sqlite import (
    SqliteStorage,
    SqliteStoragePlugin,
    SqliteTimeSeriesPlugin,
    SqliteTimeSeriesStore,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "db" / "ts.db")


async def test_cursors_and_reset(db_path: str) -> None:
    storage = await SqliteStorage.open(db_path)
    ts = SqliteTimeSeriesStore(storage.connection, storage.write_lock)

    c1 = FileCursor(
        run_id=1,
        file_path="stream0/thermo.out",
        parser_kind="converge.out",
        file_size=1024,
        mtime=1234.5,
        head_prefix=b"# CONVERGE",
        forward_offset=1024,
        backfill_start=64,
        backfill_end=512,
        header_data={"columns": ["Time", "Pressure"]},
        parser_state={"line": 10},
        status=FileStatus.BACKFILLING,
    )
    assert c1.needs_backfill
    await ts.upsert_cursor(c1)

    got = await ts.get_cursor(1, "stream0/thermo.out")
    assert got is not None
    assert got.header_data == {"columns": ["Time", "Pressure"]}
    assert got.head_prefix == b"# CONVERGE"
    assert got.needs_backfill

    c1_done = FileCursor(
        run_id=1,
        file_path="stream0/thermo.out",
        parser_kind="converge.out",
        file_size=2048,
        mtime=1235.0,
        head_prefix=b"# CONVERGE",
        forward_offset=2048,
        backfill_start=512,
        backfill_end=512,
        status=FileStatus.ACTIVE,
    )
    await ts.upsert_cursor(c1_done)
    got2 = await ts.get_cursor(1, "stream0/thermo.out")
    assert got2 is not None
    assert not got2.needs_backfill
    assert len(await ts.list_cursors(1)) == 1
    await storage.close()


async def test_chunked_append_downsampling_and_out_of_order_backfill(db_path: str) -> None:
    storage = await SqliteStorage.open(db_path)
    ts = SqliteTimeSeriesStore(storage.connection, storage.write_lock)

    key = SeriesKey("log.out", "residual.p", "", "", "Time", "s")

    # Simulate tail-first ingestion: x = [2500..4999], then backfill x = [0..2499].
    tail_x = [float(i) for i in range(2500, 5000)]
    tail_y = [math.sin(i * 0.01) for i in range(2500, 5000)]
    tail_y[500] = 42.0  # spike at x=3000

    metas = await ts.append_samples(1, {key: (tail_x, tail_y)})
    assert len(metas) == 1
    assert metas[0].point_count == 2500
    assert metas[0].last_x == 4999.0

    head_x = [float(i) for i in range(0, 2500)]
    head_y = [math.cos(i * 0.01) for i in range(0, 2500)]
    head_y[100] = -99.0  # spike at x=100

    metas2 = await ts.append_samples(1, {key: (head_x, head_y)})
    assert metas2[0].point_count == 5000
    # Backfill must not regress last_x / last_y!
    assert metas2[0].last_x == 4999.0
    assert metas2[0].last_y == pytest.approx(tail_y[-1])
    assert metas2[0].min_y == -99.0
    assert metas2[0].max_y == 42.0

    # Raw query over small window -> level 0, sorted
    window = await ts.query_series(metas2[0].id, x_min=95.0, x_max=105.0, max_points=500)
    assert window.level == 0
    assert window.x == [float(i) for i in range(95, 106)]
    assert min(window.y) == -99.0

    # Downsampled query over entire 5000-pt series -> preserves both spikes and stays ordered
    overview = await ts.query_series(metas2[0].id, max_points=200)
    assert overview.level >= 1
    assert len(overview.x) <= 200
    assert overview.x == sorted(overview.x)
    assert min(overview.y) == -99.0
    assert max(overview.y) == 42.0

    await storage.close()


async def test_nan_inf_and_reset_file_data(db_path: str) -> None:
    storage = await SqliteStorage.open(db_path)
    ts = SqliteTimeSeriesStore(storage.connection, storage.write_lock)

    k1 = SeriesKey("thermo.out", "Pressure", "", "MPa")
    k2 = SeriesKey("other.out", "Temp", "", "K")
    await ts.append_samples(
        1,
        {
            k1: ([0.1, 0.2, 0.3], [1.0, float("nan"), float("inf")]),
            k2: ([0.1, 0.2], [300.0, 301.0]),
        },
    )
    series = await ts.list_series(1)
    assert len(series) == 2

    thermo = next(s for s in series if s.file_path == "thermo.out")
    pts = await ts.query_series(thermo.id)
    assert math.isnan(pts.y[1])
    assert math.isinf(pts.y[2])

    await ts.reset_file_data(1, "thermo.out")
    remaining = await ts.list_series(1)
    assert [s.file_path for s in remaining] == ["other.out"]
    await storage.close()

    # Check sample_chunks cascading delete worked
    con = sqlite3.connect(db_path)
    chunk_series_ids = {r[0] for r in con.execute("SELECT DISTINCT series_id FROM sample_chunks")}
    con.close()
    assert chunk_series_ids == {remaining[0].id}


async def test_run_solver_state_and_plugin_reload(db_path: str) -> None:
    kernel = Kernel({"storage-sqlite": {"path": db_path}})
    await kernel.add(SqliteStoragePlugin, SqliteTimeSeriesPlugin)
    assert kernel.state("timeseries-sqlite") is PluginState.ACTIVE

    ts: TimeSeriesStore = kernel.services.require("timeseries")
    await ts.upsert_run_solver_state(
        RunSolverState(
            run_id=7,
            solver="converge",
            work_dir="/work/user1/case_a",
            output_dir="/work/user1/case_a/outputs_restart1",
            solver_state=SolverState.RUNNING,
            progress=0.25,
            current_time=0.03,
            start_time=0.02,
            end_time=0.06,
            ncyc=1000,
            warning_count=3,
        )
    )
    await ts.append_samples(7, {SeriesKey("thermo.out", "Pressure"): ([0.03], [1.2])})

    await kernel.reload("storage-sqlite")
    assert kernel.state("timeseries-sqlite") is PluginState.ACTIVE
    ts2: TimeSeriesStore = kernel.services.require("timeseries")
    state = await ts2.get_run_solver_state(7)
    assert state is not None
    assert state.solver_state is SolverState.RUNNING
    assert state.progress == 0.25
    assert len(await ts2.list_series(7)) == 1
    await kernel.stop()
