from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from simmanager.kernel import (
    PLUGIN_STATE_EVENT,
    Context,
    ContextClosedError,
    Kernel,
    KernelError,
    Plugin,
    PluginState,
)

from .helpers import make_plugin

ACTIVE, PENDING, FAILED = PluginState.ACTIVE, PluginState.PENDING, PluginState.FAILED


async def test_consumer_waits_for_provider_then_activates() -> None:
    journal: list[str] = []
    kernel = Kernel()
    await kernel.add(make_plugin("consumer", journal, inject={"svc": 1}))
    assert kernel.state("consumer") is PENDING
    assert kernel.info()[0].waiting_for == ("svc",)

    await kernel.add(make_plugin("provider", journal, provides="svc"))
    assert kernel.state("provider") is ACTIVE
    assert kernel.state("consumer") is ACTIVE
    assert journal == ["apply provider", "apply consumer"]
    await kernel.stop()


async def test_chain_of_dependencies_resolves_in_any_registration_order() -> None:
    journal: list[str] = []
    kernel = Kernel()
    await kernel.add(
        make_plugin("c", journal, inject={"b": 1}),
        make_plugin("b", journal, provides="b", inject={"a": 1}),
        make_plugin("a", journal, provides="a"),
    )
    assert [kernel.state(n) for n in "abc"] == [ACTIVE] * 3
    assert journal == ["apply a", "apply b", "apply c"]
    await kernel.stop()


async def test_provider_removed_disposes_consumer_and_it_comes_back() -> None:
    journal: list[str] = []
    kernel = Kernel()
    provider = make_plugin("provider", journal, provides="svc", effects=1)
    await kernel.add(provider, make_plugin("consumer", journal, inject={"svc": 1}, effects=2))

    await kernel.remove("provider")
    assert kernel.state("consumer") is PENDING
    assert not kernel.services.has("svc")
    # consumer torn down (LIFO) before the provider's own effects
    assert journal[-3:] == ["dispose consumer:1", "dispose consumer:0", "dispose provider:0"]

    journal.clear()
    await kernel.add(provider)
    assert kernel.state("consumer") is ACTIVE
    assert journal == ["apply provider", "apply consumer"]
    await kernel.stop()


async def test_transitive_dependents_are_deactivated() -> None:
    journal: list[str] = []
    kernel = Kernel()
    await kernel.add(
        make_plugin("a", journal, provides="a"),
        make_plugin("b", journal, provides="b", inject={"a": 1}),
        make_plugin("c", journal, inject={"b": 1}),
    )
    await kernel.remove("a")
    assert kernel.state("b") is PENDING
    assert kernel.state("c") is PENDING
    await kernel.stop()


async def test_state_transitions_are_emitted() -> None:
    journal: list[str] = []
    kernel = Kernel()
    seen: list[tuple[str, Any, Any]] = []
    kernel.bus.on(PLUGIN_STATE_EVENT, lambda p: seen.append((p["plugin"], p["old"], p["new"])))
    await kernel.add(make_plugin("p", journal))
    await kernel.remove("p")
    assert [s[2] for s in seen] == ["pending", "loading", "active", "unloading", "disposed"]


async def test_effects_run_lifo_including_async_ones() -> None:
    journal: list[str] = []
    kernel = Kernel()
    await kernel.add(make_plugin("p", journal, effects=4, async_effects=True, async_apply=True))
    journal.clear()
    await kernel.stop()
    assert journal == [f"dispose p:{i}" for i in (3, 2, 1, 0)]
    assert kernel.state("p") is PluginState.DISPOSED


async def test_failing_disposer_does_not_block_the_others() -> None:
    journal: list[str] = []

    class Broken(Plugin):
        name = "broken"

        def apply(self, ctx: Context) -> None:
            ctx.add_disposer(lambda: journal.append("first"))
            ctx.add_disposer(lambda: 1 / 0)
            ctx.add_disposer(lambda: journal.append("third"))

    kernel = Kernel()
    await kernel.add(Broken)
    await kernel.stop()
    assert journal == ["third", "first"]


async def test_early_dispose_runs_once() -> None:
    journal: list[str] = []
    handle: list[Any] = []

    class Early(Plugin):
        name = "early"

        def apply(self, ctx: Context) -> None:
            handle.append(ctx.effect(lambda: lambda: journal.append("disposed")))

    kernel = Kernel()
    await kernel.add(Early)
    handle[0]()
    handle[0]()
    await kernel.stop()
    assert journal == ["disposed"]


async def test_apply_failure_marks_failed_cleans_up_and_spares_others() -> None:
    journal: list[str] = []
    kernel = Kernel()
    await kernel.add(
        make_plugin("ok", journal, provides="ok"),
        make_plugin("bad", journal, provides="bad", effects=2, fail_on_apply=True),
        make_plugin("needs-bad", journal, inject={"bad": 1}),
    )
    assert kernel.state("ok") is ACTIVE
    assert kernel.state("bad") is FAILED
    assert "bad exploded" in (next(i for i in kernel.info() if i.name == "bad").error or "")
    assert kernel.state("needs-bad") is PENDING
    # partial effects of the failed activation were disposed, LIFO
    assert journal[-2:] == ["dispose bad:1", "dispose bad:0"]
    assert not kernel.services.has("bad")
    await kernel.stop()


async def test_reload_retries_a_failed_plugin() -> None:
    journal: list[str] = []
    calls = {"n": 0}

    class Flaky(Plugin):
        name = "flaky"

        def apply(self, ctx: Context) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first time fails")

    kernel = Kernel()
    await kernel.add(Flaky)
    assert kernel.state("flaky") is FAILED
    await kernel.reload("flaky")
    assert kernel.state("flaky") is ACTIVE
    await kernel.stop()
    del journal


async def test_reload_active_provider_cycles_consumers() -> None:
    journal: list[str] = []
    kernel = Kernel()
    await kernel.add(
        make_plugin("provider", journal, provides="svc"),
        make_plugin("consumer", journal, inject={"svc": 1}),
    )
    journal.clear()
    await kernel.reload("provider")
    assert journal == ["apply provider", "apply consumer"]
    assert kernel.state("consumer") is ACTIVE
    await kernel.stop()


async def test_crashing_task_fails_plugin_and_deactivates_dependents() -> None:
    journal: list[str] = []
    started = asyncio.Event()

    class Crashy(Plugin):
        name = "crashy"
        provides = "svc"

        def apply(self, ctx: Context) -> object:
            async def loop() -> None:
                started.set()
                await asyncio.sleep(0)
                raise ValueError("boom")

            ctx.task(loop(), name="loop")
            ctx.add_disposer(lambda: journal.append("crashy disposed"))
            return object()

    kernel = Kernel()
    await kernel.add(Crashy, make_plugin("consumer", journal, inject={"svc": 1}, effects=1))
    await started.wait()
    await asyncio.sleep(0.01)
    await kernel.wait_idle()
    assert kernel.state("crashy") is FAILED
    assert "boom" in (next(i for i in kernel.info() if i.name == "crashy").error or "")
    assert kernel.state("consumer") is PENDING
    assert journal == ["apply consumer", "dispose consumer:0", "crashy disposed"]
    await kernel.stop()


async def test_tasks_are_cancelled_on_deactivation() -> None:
    cancelled = asyncio.Event()

    class Looper(Plugin):
        name = "looper"

        def apply(self, ctx: Context) -> None:
            async def loop() -> None:
                try:
                    await asyncio.sleep(3600)
                finally:
                    cancelled.set()

            ctx.task(loop(), name="loop")

    kernel = Kernel()
    await kernel.add(Looper)
    await asyncio.sleep(0)
    await kernel.stop()
    assert cancelled.is_set()
    assert kernel.state("looper") is PluginState.DISPOSED


async def test_contract_version_mismatch_fails_consumer() -> None:
    journal: list[str] = []
    kernel = Kernel()
    await kernel.add(
        make_plugin("provider", journal, provides="svc", version=2),
        make_plugin("consumer", journal, inject={"svc": 1}),
    )
    assert kernel.state("consumer") is FAILED
    error = next(i for i in kernel.info() if i.name == "consumer").error or ""
    assert "v1" in error and "v2" in error
    await kernel.stop()


async def test_two_providers_for_same_key_second_fails() -> None:
    journal: list[str] = []
    kernel = Kernel()
    await kernel.add(
        make_plugin("first", journal, provides="svc"),
        make_plugin("second", journal, provides="svc"),
    )
    assert kernel.state("first") is ACTIVE
    assert kernel.state("second") is FAILED
    assert kernel.services.require("svc").owner == "first"
    await kernel.stop()


async def test_provides_but_returns_none_fails() -> None:
    class Forgetful(Plugin):
        name = "forgetful"
        provides = "svc"

        def apply(self, ctx: Context) -> None:
            return None

    kernel = Kernel()
    await kernel.add(Forgetful)
    assert kernel.state("forgetful") is FAILED
    await kernel.stop()


async def test_invalid_plugin_config_fails_with_key_path() -> None:
    class Schema(BaseModel):
        model_config = ConfigDict(extra="forbid")
        interval_s: float = Field(default=1, gt=0)

    journal: list[str] = []
    kernel = Kernel({"cfg": {"interval_s": -3, "enabled": True}})
    await kernel.add(make_plugin("cfg", journal, schema=Schema))
    assert kernel.state("cfg") is FAILED
    error = kernel.info()[0].error or ""
    assert "plugins.cfg.interval_s" in error
    assert journal == []


async def test_valid_plugin_config_reaches_context() -> None:
    class Schema(BaseModel):
        interval_s: float = 1.0

    seen: list[float] = []

    class Configured(Plugin):
        name = "configured"
        config_schema = Schema

        def apply(self, ctx: Context) -> None:
            assert isinstance(ctx.config, Schema)
            seen.append(ctx.config.interval_s)

    kernel = Kernel({"configured": {"interval_s": 7}})
    await kernel.add(Configured)
    assert seen == [7.0]
    await kernel.stop()


async def test_context_is_closed_after_deactivation() -> None:
    contexts: list[Context] = []

    class Keeper(Plugin):
        name = "keeper"

        def apply(self, ctx: Context) -> None:
            contexts.append(ctx)

    kernel = Kernel()
    await kernel.add(Keeper)
    await kernel.stop()
    with pytest.raises(ContextClosedError):
        contexts[0].on("x", lambda p: None)


async def test_listeners_removed_on_deactivation() -> None:
    hits: list[Any] = []

    class Listener(Plugin):
        name = "listener"

        def apply(self, ctx: Context) -> None:
            ctx.on("ping", hits.append)

    kernel = Kernel()
    await kernel.add(Listener)
    await kernel.bus.emit("ping", 1)
    await kernel.remove("listener")
    await kernel.bus.emit("ping", 2)
    assert hits == [1]
    assert kernel.bus.subscribers("ping") == 0


async def test_duplicate_and_unknown_names() -> None:
    journal: list[str] = []
    kernel = Kernel()
    p = make_plugin("p", journal)
    await kernel.add(p)
    with pytest.raises(KernelError):
        await kernel.add(p)
    with pytest.raises(KernelError):
        kernel.state("nope")
    await kernel.stop()
