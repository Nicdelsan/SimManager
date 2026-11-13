"""Example plugins showing the kernel concepts. Disabled by default.

Enable them in the user config to try the lifecycle:

    plugins:
      example-clock: { enabled: true, interval_s: 2 }
      example-ticker: { enabled: true }
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from simmanager.kernel import Context, Plugin

TICK_EVENT = "example.tick"


class Clock:
    """The `clock` service."""

    def __init__(self) -> None:
        self.ticks = 0

    def now(self) -> datetime:
        return datetime.now(UTC)


class ClockConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_s: float = Field(default=5.0, gt=0)


class ExampleClockPlugin(Plugin):
    name = "example-clock"
    provides = "clock"
    config_schema = ClockConfig
    description = "Example provider: publishes `clock` and emits `example.tick` periodically."

    def apply(self, ctx: Context) -> Clock:
        assert isinstance(ctx.config, ClockConfig)
        clock = Clock()
        interval = ctx.config.interval_s

        async def tick_loop() -> None:
            while True:
                await asyncio.sleep(interval)
                clock.ticks += 1
                await ctx.emit(TICK_EVENT, {"n": clock.ticks, "at": clock.now().isoformat()})

        ctx.task(tick_loop(), name="tick")
        return clock


class ExampleTickerPlugin(Plugin):
    name = "example-ticker"
    inject: ClassVar[Mapping[str, int]] = {"clock": 1}
    description = "Example consumer: logs every tick; records it in storage when available."

    def apply(self, ctx: Context) -> None:
        async def on_tick(payload: dict[str, object]) -> None:
            ctx.log.info("tick %s", payload["n"])
            storage = ctx.services.get("storage")  # optional dependency, looked up at use time
            if storage is not None:
                await storage.append_event("example.tick", payload, source=ctx.plugin_name)

        ctx.on(TICK_EVENT, on_tick)


__all__ = ["Clock", "ExampleClockPlugin", "ExampleTickerPlugin"]
