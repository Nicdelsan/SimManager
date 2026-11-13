"""Live WebSocket traffic: durable events, progress, throttled plot points, reloads."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from simmanager.contracts import storage as storage_contract
from simmanager.contracts import timeseries as ts_contract
from simmanager.contracts.storage import Storage
from simmanager.contracts.timeseries import SeriesKey, TimeSeriesStore
from simmanager.plugins.storage_sqlite import SqliteStoragePlugin, SqliteTimeSeriesPlugin
from simmanager.plugins.ui_web import live
from simmanager.plugins.ui_web.live import RateLimit, parse_subscription

from .webclient import Web, recv_type, running_web, ws_connect

INTERVAL_S = 0.3
KEY = SeriesKey(file_path="/case/thermo.out", name="Pressure", unit="MPa")


@pytest.fixture
async def web(tmp_path: Path) -> AsyncIterator[Web]:
    config = {
        "storage-sqlite": {"path": str(tmp_path / "sim.db")},
        "ui-web": {"port": 0, "live_interval_s": INTERVAL_S},
    }
    async with running_web(SqliteStoragePlugin, SqliteTimeSeriesPlugin, config=config) as w:
        yield w


async def _run_with_series(web: Web, n: int) -> tuple[int, int, TimeSeriesStore]:
    storage: Storage = web.kernel.services.require(storage_contract.SERVICE_KEY)
    ts: TimeSeriesStore = web.kernel.services.require(ts_contract.SERVICE_KEY)
    run = await storage.create_run(cluster_id="c1", work_dir="/case")
    metas = await ts.append_samples(run.id, {KEY: ([float(i) for i in range(n)], [1.0] * n)})
    return run.id, metas[0].id, ts


async def _updated(web: Web, run_id: int) -> None:
    await web.kernel.bus.emit("series.updated", {"run_id": run_id, "files": [KEY.file_path]})


def test_parse_subscription() -> None:
    ok = parse_subscription({"run_id": 1, "series": [{"id": 2, "last_x": 3}, {"id": 4}]})
    assert ok == (1, {2: 3.0, 4: float("-inf")})
    for bad in (
        {"run_id": "1", "series": []},
        {"run_id": True, "series": []},
        {"run_id": 1, "series": {}},
        {"run_id": 1, "series": [{"id": "2"}]},
        {"run_id": 1, "series": [{"id": 2, "last_x": True}]},
        {"run_id": 1, "series": [5]},
    ):
        assert parse_subscription(bad) is None, bad


def test_rate_limit() -> None:
    now = [0.0]
    rl = RateLimit(1.0, lambda: now[0])
    assert rl.allow("a") and not rl.allow("a") and rl.allow("b")
    now[0] = 1.0
    assert rl.allow("a")


async def test_points_are_pushed_once_per_interval(web: Web) -> None:
    run_id, sid, ts = await _run_with_series(web, 10)
    async with ws_connect(web) as ws:
        await recv_type(ws, "hello")
        await ws.send(
            json.dumps(
                {"type": "subscribe", "run_id": run_id, "series": [{"id": sid, "last_x": 9}]}
            )
        )
        await recv_type(ws, "series.updated")  # catch-up flush: nothing new yet
        await asyncio.sleep(INTERVAL_S * 1.5)  # let the interval expire

        # First update after a quiet period: sent right away, only points after last_x.
        await ts.append_samples(run_id, {KEY: ([10.0, 11.0], [2.0, 3.0])})
        await _updated(web, run_id)
        msg = await recv_type(ws, "points")
        assert msg == {
            "type": "points",
            "run_id": run_id,
            "series_id": sid,
            "x": [10.0, 11.0],
            "y": [2.0, 3.0],
        }
        loop = asyncio.get_running_loop()
        t0 = loop.time()

        # Two batches inside the interval -> one later message with both.
        await ts.append_samples(run_id, {KEY: ([12.0], [4.0])})
        await _updated(web, run_id)
        await ts.append_samples(run_id, {KEY: ([13.0], [5.0])})
        await _updated(web, run_id)
        msg = await recv_type(ws, "points")
        assert (msg["x"], msg["y"]) == ([12.0, 13.0], [4.0, 5.0])
        assert loop.time() - t0 >= INTERVAL_S * 0.5  # waited for the interval

        # Other runs' updates are not sent to this browser.
        other, _, _ = await _run_with_series(web, 3)
        await _updated(web, other)
        await ws.send(json.dumps({"type": "unsubscribe", "run_id": run_id}))
        await ts.append_samples(run_id, {KEY: ([14.0], [6.0])})
        await _updated(web, run_id)
        with pytest.raises((TimeoutError, AssertionError)):
            await recv_type(ws, "points", timeout_s=INTERVAL_S * 3)


async def test_big_jump_asks_for_reload(web: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "MAX_LIVE_POINTS", 5)
    run_id, sid, ts = await _run_with_series(web, 3)
    async with ws_connect(web) as ws:
        await recv_type(ws, "hello")
        await ws.send(
            json.dumps(
                {"type": "subscribe", "run_id": run_id, "series": [{"id": sid, "last_x": 2}]}
            )
        )
        await ts.append_samples(run_id, {KEY: ([float(i) for i in range(3, 20)], [0.0] * 17)})
        await _updated(web, run_id)
        msg = await recv_type(ws, "series.reload")
        assert msg["series_id"] == sid


async def test_invalid_subscribe_gets_error(web: Web) -> None:
    async with ws_connect(web) as ws:
        await recv_type(ws, "hello")
        await ws.send(json.dumps({"type": "subscribe", "run_id": "x"}))
        assert (await recv_type(ws, "error"))["detail"] == "invalid subscribe message"


async def test_durable_events_progress_and_backfill_are_relayed(web: Web) -> None:
    storage: Storage = web.kernel.services.require(storage_contract.SERVICE_KEY)
    async with ws_connect(web) as ws:
        await recv_type(ws, "hello")
        await storage.append_event("solver.finished", {"x": 1}, source="t", run_id=None)
        msg: dict[str, Any] = await recv_type(ws, "event")
        assert msg["kind"] == "solver.finished" and msg["payload"] == {"x": 1}

        await web.kernel.bus.emit("run.progress", {"run_id": 1, "progress": 0.5})
        assert (await recv_type(ws, "run.progress"))["progress"] == 0.5

        for read in (1, 2, 3):
            await web.kernel.bus.emit(
                "backfill.progress",
                {"run_id": 1, "file_path": "/f", "bytes_read": read, "done": False},
            )
        await web.kernel.bus.emit(
            "backfill.progress", {"run_id": 1, "file_path": "/f", "bytes_read": 4, "done": True}
        )
        first = await recv_type(ws, "backfill.progress")
        last = await recv_type(ws, "backfill.progress")
        assert (first["bytes_read"], last["bytes_read"], last["done"]) == (1, 4, True)
