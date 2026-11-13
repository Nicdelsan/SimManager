"""Live plot data: push only the new points of the series a browser is showing.

A browser loads a plot through REST, then sends
`{"type": "subscribe", "run_id": 3, "series": [{"id": 12, "last_x": 0.0042}, ...]}`.
On `series.updated` for that run the server sends `{"type": "points", "series_id", "x",
"y"}` with the points after `last_x`, at most once per `interval_s` per run. When more
than `MAX_LIVE_POINTS` arrived at once (e.g. after a disconnection) it sends
`{"type": "series.reload", "series_id"}` and the browser reloads that plot via REST.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from simmanager.contracts import timeseries as ts_contract
from simmanager.contracts.timeseries import TimeSeriesStore
from simmanager.contracts.ui import Message
from simmanager.kernel.services import ServiceView

from .hub import Client, Hub

MAX_LIVE_POINTS = 5000
MAX_SUBSCRIBED_SERIES = 1000
"""Per browser; a defensive bound, a real page shows a few dozen."""
_RAW = 1 << 40
"""`max_points` large enough to always get level 0 without downsampling."""
SUBS = "live.subs"
"""Key in `Client.state`: {run_id: {series_id: last_x}}."""

Subs = dict[int, dict[int, float]]


class RateLimit:
    """At most one event per key every `interval_s` (monotonic clock)."""

    def __init__(self, interval_s: float, clock: Callable[[], float]) -> None:
        self._interval_s = interval_s
        self._clock = clock
        self._last: dict[Any, float] = {}

    def allow(self, key: Any) -> bool:
        now = self._clock()
        if now - self._last.get(key, -1e18) < self._interval_s:
            return False
        self._last[key] = now
        return True


def _subs(client: Client) -> Subs:
    return client.state.setdefault(SUBS, {})  # type: ignore[no-any-return]


def parse_subscription(message: Message) -> tuple[int, dict[int, float]] | None:
    run_id = message.get("run_id")
    series = message.get("series")
    if type(run_id) is not int or not isinstance(series, list):
        return None
    out: dict[int, float] = {}
    for item in series[:MAX_SUBSCRIBED_SERIES]:
        if not isinstance(item, dict):
            return None
        sid, last_x = item.get("id"), item.get("last_x")
        if type(sid) is not int or isinstance(last_x, bool):
            return None
        if not isinstance(last_x, int | float):
            last_x = float("-inf")  # empty plot: everything is new
        out[sid] = float(last_x)
    return run_id, out


class Live:
    def __init__(
        self,
        hub: Hub,
        services: ServiceView,
        log: logging.Logger,
        *,
        interval_s: float = 1.0,
    ) -> None:
        self._hub = hub
        self._services = services
        self._log = log
        self._interval_s = interval_s
        self._timers: dict[int, asyncio.Task[None]] = {}
        self._last_flush: dict[int, float] = {}

    def install(self) -> list[Callable[[], None]]:
        return [
            self._hub.on_client_message("subscribe", self._on_subscribe),
            self._hub.on_client_message("unsubscribe", self._on_unsubscribe),
        ]

    async def close(self) -> None:
        timers = list(self._timers.values())
        self._timers.clear()
        for task in timers:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -------------------------------------------------------------- messages
    async def _on_subscribe(self, client: Client, message: Message) -> None:
        parsed = parse_subscription(message)
        if parsed is None:
            client.send({"type": "error", "detail": "invalid subscribe message"})
            return
        run_id, series = parsed
        subs = _subs(client)
        subs[run_id] = series
        total = sum(len(s) for s in subs.values())
        if total > MAX_SUBSCRIBED_SERIES:
            del subs[run_id]
            client.send({"type": "error", "detail": "too many subscribed series"})
            return
        self.series_updated(run_id)  # catch up with anything newer than the REST load

    async def _on_unsubscribe(self, client: Client, message: Message) -> None:
        run_id = message.get("run_id")
        if run_id is None:
            _subs(client).clear()
        elif type(run_id) is int:
            _subs(client).pop(run_id, None)

    # ----------------------------------------------------------------- flow
    def series_updated(self, run_id: int) -> None:
        """New data for a run: flush now, or as soon as the per-run interval allows."""
        if run_id in self._timers:
            return  # a flush is already scheduled; it will read the latest data
        loop = asyncio.get_running_loop()
        delay = max(0.0, self._last_flush.get(run_id, -1e9) + self._interval_s - loop.time())
        self._timers[run_id] = loop.create_task(self._flush_later(run_id, delay))

    async def _flush_later(self, run_id: int, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
        finally:
            self._timers.pop(run_id, None)
        self._last_flush[run_id] = asyncio.get_running_loop().time()
        try:
            await self._flush(run_id)
        except Exception as exc:
            self._log.warning("live update of run %s failed: %s", run_id, exc)

    async def _flush(self, run_id: int) -> None:
        ts: TimeSeriesStore | None = self._services.get(ts_contract.SERVICE_KEY)
        if ts is None:
            return
        cache: dict[tuple[int, float], Any] = {}
        for client in self._hub.clients():
            series = _subs(client).get(run_id)
            if series is None:
                continue
            client.send({"type": "series.updated", "run_id": run_id})  # new series may exist
            for sid, last_x in list(series.items()):
                key = (sid, last_x)
                if key not in cache:
                    cache[key] = await self._new_points(ts, sid, last_x)
                result = cache[key]
                if result is None:
                    continue
                if result == "reload":
                    client.send({"type": "series.reload", "run_id": run_id, "series_id": sid})
                    series.pop(sid, None)  # the browser re-subscribes after reloading
                    continue
                xs, ys = result
                client.send(
                    {"type": "points", "run_id": run_id, "series_id": sid, "x": xs, "y": ys}
                )
                series[sid] = xs[-1]

    @staticmethod
    async def _new_points(
        ts: TimeSeriesStore, series_id: int, last_x: float
    ) -> tuple[list[float], list[float]] | str | None:
        x_min = None if last_x == float("-inf") else last_x
        try:
            # Raw points (no downsampling): only the tail after last_x is read.
            pts = await ts.query_series(series_id, x_min=x_min, max_points=_RAW)
        except KeyError:
            return None
        pairs = [(x, y) for x, y in zip(pts.x, pts.y, strict=True) if x > last_x]
        if not pairs:
            return None
        if len(pairs) > MAX_LIVE_POINTS:
            return "reload"  # too many new points for a live append
        return [p[0] for p in pairs], [p[1] for p in pairs]
