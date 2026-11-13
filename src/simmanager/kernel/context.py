"""Per-plugin context: access to services, events and effect registration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from pydantic import BaseModel

from .effects import Disposer, EffectStack
from .errors import ContextClosedError
from .events import EventBus, Handler, WaterfallHandler
from .services import ServiceView

TaskCrashCallback = Callable[["Context", BaseException], None]


class Context:
    """Created by the kernel for each plugin activation. Closed when the plugin is deactivated."""

    def __init__(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
        services: ServiceView,
        bus: EventBus,
        on_task_crash: TaskCrashCallback,
    ) -> None:
        self.plugin_name = plugin_name
        self.config = config
        self.services = services
        self.log = logging.getLogger(f"simmanager.plugin.{plugin_name}")
        self._bus = bus
        self._on_task_crash = on_task_crash
        self.effects = EffectStack(self.log)
        self._closed = False

    # --------------------------------------------------------------- effects
    def effect(self, setup: Callable[[], Disposer]) -> Disposer:
        """Run `setup` now and register the disposer it returns.

        The returned callable disposes the effect early; otherwise it runs on deactivation.
        """
        self._check_open()
        return self.effects.push(setup(), label=getattr(setup, "__qualname__", ""))

    def add_disposer(self, disposer: Disposer, label: str = "") -> Disposer:
        """Register cleanup for a resource already set up (e.g. after an `await`)."""
        self._check_open()
        return self.effects.push(disposer, label)

    def task(self, coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        """Start a background task owned by this plugin.

        The task is cancelled and awaited on deactivation. If it ends with an unhandled
        exception the plugin is marked FAILED: loops must handle per-iteration errors themselves.
        """
        self._check_open()
        task = asyncio.create_task(coro, name=f"{self.plugin_name}:{name}")

        def _done(t: asyncio.Task[None]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self._on_task_crash(self, exc)

        task.add_done_callback(_done)

        async def _cancel() -> None:
            task.remove_done_callback(_done)
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        self.effects.push(_cancel, label=f"task {name}")
        return task

    # ---------------------------------------------------------------- events
    def on(self, event: str, handler: Handler, *, priority: int = 0) -> Disposer:
        self._check_open()
        return self.effects.push(
            self._bus.on(event, handler, priority=priority, owner=self.plugin_name),
            label=f"on {event}",
        )

    def waterfall_on(self, event: str, handler: WaterfallHandler, *, priority: int = 0) -> Disposer:
        self._check_open()
        return self.effects.push(
            self._bus.waterfall_on(event, handler, priority=priority, owner=self.plugin_name),
            label=f"waterfall_on {event}",
        )

    async def emit(self, event: str, payload: Any = None) -> None:
        await self._bus.emit(event, payload)

    async def waterfall(self, event: str, payload: Any) -> Any:
        return await self._bus.waterfall(event, payload)

    # --------------------------------------------------------------- closing
    @property
    def closed(self) -> bool:
        return self._closed

    async def _close(self) -> list[BaseException]:
        self._closed = True
        return await self.effects.dispose_all()

    def _check_open(self) -> None:
        if self._closed:
            raise ContextClosedError(f"context of plugin {self.plugin_name!r} is closed")
