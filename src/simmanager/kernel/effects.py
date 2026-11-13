"""Effects: resources set up by a plugin and torn down, in LIFO order, on deactivation."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable

Disposer = Callable[[], "Awaitable[None] | None"]
"""A callable that releases a resource. May be sync or async."""


class _OnceDisposer:
    """Wraps a disposer so that it runs at most once and unregisters itself from its stack."""

    def __init__(self, stack: EffectStack, disposer: Disposer, label: str) -> None:
        self._stack = stack
        self._disposer = disposer
        self.label = label
        self.done = False

    def __call__(self) -> Awaitable[None] | None:
        if self.done:
            return None
        self.done = True
        self._stack._forget(self)
        return self._disposer()


class EffectStack:
    """LIFO stack of disposers owned by one plugin activation."""

    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger
        self._items: list[_OnceDisposer] = []

    def __len__(self) -> int:
        return len(self._items)

    def push(self, disposer: Disposer, label: str = "") -> Disposer:
        """Register a disposer. The returned callable disposes it early (idempotent)."""
        handle = _OnceDisposer(self, disposer, label or _describe(disposer))
        self._items.append(handle)
        return handle

    def _forget(self, handle: _OnceDisposer) -> None:
        if handle in self._items:
            self._items.remove(handle)

    async def dispose_all(self) -> list[BaseException]:
        """Run pending disposers, newest first. Errors are logged and collected, never raised."""
        errors: list[BaseException] = []
        while self._items:
            handle = self._items[-1]
            try:
                result = handle()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # a broken disposer must not block the others
                self._log.exception("disposer %r failed", handle.label)
                errors.append(exc)
        return errors


def _describe(fn: object) -> str:
    return getattr(fn, "__qualname__", None) or repr(fn)
