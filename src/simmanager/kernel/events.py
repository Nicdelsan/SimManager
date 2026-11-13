"""In-memory event bus with plain events and waterfall (transforming) events.

The bus is NOT durable. Facts that must survive a restart go to the storage service.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import count
from typing import Any

from .errors import Blocked

Handler = Callable[[Any], "Awaitable[None] | None"]
WaterfallHandler = Callable[[Any], Any]
"""Receives the payload, returns the (possibly transformed) payload, sync or async.
Raise `Blocked` to reject the payload."""

_seq = count()


@dataclass(order=True)
class _Subscription:
    sort_key: tuple[int, int]
    handler: Callable[[Any], Any] = field(compare=False)
    owner: str = field(compare=False)


class EventBus:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or logging.getLogger("simmanager.kernel.events")
        self._plain: dict[str, list[_Subscription]] = {}
        self._waterfall: dict[str, list[_Subscription]] = {}

    # ------------------------------------------------------------------ plain
    def on(
        self, event: str, handler: Handler, *, priority: int = 0, owner: str = ""
    ) -> Callable[[], None]:
        """Subscribe. Higher priority runs first; equal priority runs in subscription order."""
        return self._add(self._plain, event, handler, priority, owner)

    async def emit(self, event: str, payload: Any = None) -> None:
        """Call every handler in order. A failing handler is logged and skipped."""
        for sub in list(self._plain.get(event, ())):
            try:
                result = sub.handler(payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                self._log.exception("handler of %r owned by %r failed", event, sub.owner)

    # -------------------------------------------------------------- waterfall
    def waterfall_on(
        self, event: str, handler: WaterfallHandler, *, priority: int = 0, owner: str = ""
    ) -> Callable[[], None]:
        return self._add(self._waterfall, event, handler, priority, owner)

    async def waterfall(self, event: str, payload: Any) -> Any:
        """Pass the payload through every handler in order and return the result.

        `Blocked` propagates to the caller. Any other handler error is logged and the
        handler is skipped (the payload continues unchanged).
        """
        for sub in list(self._waterfall.get(event, ())):
            try:
                result = sub.handler(payload)
                if inspect.isawaitable(result):
                    result = await result
            except Blocked:
                raise
            except Exception:
                self._log.exception("waterfall handler of %r owned by %r failed", event, sub.owner)
                continue
            payload = result
        return payload

    # ---------------------------------------------------------------- helpers
    def subscribers(self, event: str) -> int:
        return len(self._plain.get(event, ())) + len(self._waterfall.get(event, ()))

    def _add(
        self,
        table: dict[str, list[_Subscription]],
        event: str,
        handler: Callable[[Any], Any],
        priority: int,
        owner: str,
    ) -> Callable[[], None]:
        sub = _Subscription((-priority, next(_seq)), handler, owner)
        subs = table.setdefault(event, [])
        subs.append(sub)
        subs.sort()

        def unsubscribe() -> None:
            if sub in subs:
                subs.remove(sub)

        return unsubscribe
