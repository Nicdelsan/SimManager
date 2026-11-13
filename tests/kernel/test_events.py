from __future__ import annotations

from typing import Any

import pytest

from simmanager.kernel import Blocked, Context, EventBus, Kernel, Plugin


async def test_emit_order_by_priority_then_subscription() -> None:
    bus = EventBus()
    calls: list[str] = []
    bus.on("e", lambda p: calls.append("low"), priority=-1)
    bus.on("e", lambda p: calls.append("first"))
    bus.on("e", lambda p: calls.append("second"))
    bus.on("e", lambda p: calls.append("high"), priority=10)
    await bus.emit("e")
    assert calls == ["high", "first", "second", "low"]


async def test_emit_isolates_failing_handler() -> None:
    bus = EventBus()
    calls: list[Any] = []

    async def ok(p: Any) -> None:
        calls.append(p)

    bus.on("e", lambda p: 1 / 0)
    bus.on("e", ok)
    await bus.emit("e", 42)
    assert calls == [42]


async def test_waterfall_transforms_in_chain() -> None:
    bus = EventBus()

    async def add_note(p: dict[str, Any]) -> dict[str, Any]:
        return {**p, "note": "lowered CFL"}

    bus.waterfall_on(
        "run.before_relaunch_snapshot", lambda p: {**p, "files": [*p["files"], "x.in"]}
    )
    bus.waterfall_on("run.before_relaunch_snapshot", add_note)
    out = await bus.waterfall("run.before_relaunch_snapshot", {"files": ["inputs.in"]})
    assert out == {"files": ["inputs.in", "x.in"], "note": "lowered CFL"}


async def test_waterfall_blocked_stops_chain() -> None:
    bus = EventBus()
    later: list[Any] = []

    def veto(p: Any) -> Any:
        raise Blocked("too big")

    bus.waterfall_on("e", veto, priority=5)
    bus.waterfall_on("e", lambda p: later.append(p) or p)
    with pytest.raises(Blocked) as info:
        await bus.waterfall("e", 1)
    assert info.value.reason == "too big"
    assert later == []


async def test_waterfall_skips_broken_handler() -> None:
    bus = EventBus()
    bus.waterfall_on("e", lambda p: p + 1)
    bus.waterfall_on("e", lambda p: 1 / 0)
    bus.waterfall_on("e", lambda p: p * 10)
    assert await bus.waterfall("e", 1) == 20


async def test_waterfall_without_handlers_returns_payload() -> None:
    assert await EventBus().waterfall("nothing", {"a": 1}) == {"a": 1}


async def test_plugin_waterfall_handler_removed_on_unload() -> None:
    class Upper(Plugin):
        name = "upper"

        def apply(self, ctx: Context) -> None:
            ctx.waterfall_on("text", lambda s: s.upper())

    kernel = Kernel()
    await kernel.add(Upper)
    assert await kernel.bus.waterfall("text", "abc") == "ABC"
    await kernel.remove("upper")
    assert await kernel.bus.waterfall("text", "abc") == "abc"
