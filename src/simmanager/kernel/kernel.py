"""The kernel: plugin lifecycle and reactive dependency injection.

Rules enforced here:
- a plugin whose required services are missing stays PENDING and activates when they appear;
- when a provider goes away, its consumers are deactivated (effects disposed LIFO) and go
  back to PENDING;
- a failing plugin becomes FAILED and never takes the process down.

The kernel knows nothing about CFD, clusters or storage.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import count
from typing import Any

from pydantic import ValidationError

from .config import format_validation_error
from .context import Context
from .errors import ConfigError, ContractVersionError, KernelError
from .events import EventBus
from .plugin import Plugin, PluginState
from .services import ServiceEntry, ServiceRegistry, ServiceView

PLUGIN_STATE_EVENT = "kernel.plugin_state"


@dataclass(frozen=True)
class PluginInfo:
    name: str
    state: PluginState
    provides: str | None
    inject: Mapping[str, int]
    waiting_for: tuple[str, ...]
    error: str | None
    description: str


@dataclass
class _Record:
    cls: type[Plugin]
    state: PluginState = PluginState.PENDING
    error: str | None = None
    ctx: Context | None = None
    activation_seq: int = -1

    @property
    def name(self) -> str:
        return self.cls.name


class Kernel:
    def __init__(
        self,
        plugin_config: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        bus: EventBus | None = None,
    ) -> None:
        self.log = logging.getLogger("simmanager.kernel")
        self.registry = ServiceRegistry()
        self.services = ServiceView(self.registry)
        self.bus = bus or EventBus()
        self._plugin_config = dict(plugin_config or {})
        self._records: dict[str, _Record] = {}
        self._lock = asyncio.Lock()
        self._seq = count()
        self._crash_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ public API
    async def add(self, *classes: type[Plugin]) -> None:
        """Register plugins and activate every one whose dependencies are satisfied."""
        async with self._lock:
            for cls in classes:
                name = getattr(cls, "name", None)
                if not isinstance(name, str) or not name:
                    raise KernelError(f"{cls!r} has no `name`")
                if name in self._records:
                    raise KernelError(f"plugin {name!r} is already registered")
                self._records[name] = _Record(cls)
                await self._emit_state(self._records[name], None)
            await self._resolve()

    async def remove(self, name: str) -> None:
        async with self._lock:
            await self._deactivate(self._get(name), PluginState.DISPOSED)
            del self._records[name]

    async def reload(self, name: str) -> None:
        """Deactivate (if active) and re-activate a plugin. Also clears FAILED."""
        async with self._lock:
            record = self._get(name)
            await self._deactivate(record, PluginState.PENDING)
            await self._resolve()

    async def stop(self) -> None:
        """Deactivate everything, consumers before providers."""
        async with self._lock:
            for record in sorted(self._records.values(), key=lambda r: -r.activation_seq):
                await self._deactivate(record, PluginState.DISPOSED)
        await self.wait_idle()

    async def wait_idle(self) -> None:
        """Wait until crash handling scheduled by failed tasks has completed."""
        while self._crash_tasks:
            await asyncio.gather(*list(self._crash_tasks), return_exceptions=True)
            # gather() on finished tasks may return without yielding: give the loop a turn
            # so their done-callbacks (which empty the set) can run.
            await asyncio.sleep(0)

    def state(self, name: str) -> PluginState:
        return self._get(name).state

    def info(self) -> list[PluginInfo]:
        return [self._info(r) for r in self._records.values()]

    # ------------------------------------------------------------ resolution
    async def _resolve(self) -> None:
        progress = True
        while progress:
            progress = False
            for record in list(self._records.values()):
                if record.state is not PluginState.PENDING:
                    continue
                if self._missing(record):
                    continue
                mismatch = self._version_mismatch(record)
                if mismatch:
                    await self._set_state(record, PluginState.FAILED, mismatch)
                    continue
                await self._activate(record)
                progress = True

    def _missing(self, record: _Record) -> tuple[str, ...]:
        return tuple(k for k in record.cls.inject if self.registry.entry(k) is None)

    def _version_mismatch(self, record: _Record) -> str | None:
        for key, wanted in record.cls.inject.items():
            entry = self.registry.entry(key)
            if entry is not None and entry.contract_version != wanted:
                return str(
                    ContractVersionError(
                        f"needs {key!r} contract v{wanted}, provider {entry.provider!r} "
                        f"offers v{entry.contract_version}"
                    )
                )
        return None

    async def _activate(self, record: _Record) -> None:
        cls = record.cls
        await self._set_state(record, PluginState.LOADING)
        raw = dict(self._plugin_config.get(cls.name, {}))
        raw.pop("enabled", None)
        try:
            config = cls.config_schema.model_validate(raw)
        except ValidationError as exc:
            error = ConfigError(format_validation_error(exc, f"plugins.{cls.name}"))
            await self._set_state(record, PluginState.FAILED, str(error))
            return

        ctx = Context(
            plugin_name=cls.name,
            config=config,
            services=self.services,
            bus=self.bus,
            on_task_crash=self._on_task_crash,
        )
        record.ctx = ctx
        try:
            result = cls().apply(ctx)
            if inspect.isawaitable(result):
                result = await result
            if cls.provides:
                if result is None:
                    raise KernelError(
                        f"declares provides={cls.provides!r} but apply() returned None"
                    )
                self.registry.register(
                    ServiceEntry(cls.provides, result, cls.name, cls.contract_version)
                )
        except Exception as exc:
            self.log.exception("plugin %r failed to activate", cls.name)
            await ctx._close()
            record.ctx = None
            await self._set_state(record, PluginState.FAILED, f"{type(exc).__name__}: {exc}")
            return

        record.activation_seq = next(self._seq)
        await self._set_state(record, PluginState.ACTIVE)

    async def _deactivate(
        self, record: _Record, final: PluginState, error: str | None = None
    ) -> None:
        if record.state is PluginState.ACTIVE and record.ctx is not None:
            provided = record.cls.provides
            if provided:
                dependents = [
                    r
                    for r in self._records.values()
                    if r.state is PluginState.ACTIVE and provided in r.cls.inject
                ]
                for dep in sorted(dependents, key=lambda r: -r.activation_seq):
                    await self._deactivate(dep, PluginState.PENDING)
                entry = self.registry.entry(provided)
                if entry is not None and entry.provider == record.name:
                    self.registry.unregister(provided)

            await self._set_state(record, PluginState.UNLOADING)
            ctx, record.ctx = record.ctx, None
            await ctx._close()
            record.activation_seq = -1
            await self._set_state(record, PluginState.DISPOSED)

        if record.state is not final or error:
            await self._set_state(record, final, error)

    # --------------------------------------------------------- task crashes
    def _on_task_crash(self, ctx: Context, exc: BaseException) -> None:
        self.log.error("task of plugin %r crashed: %r", ctx.plugin_name, exc, exc_info=exc)
        task = asyncio.get_running_loop().create_task(self._handle_crash(ctx, exc))
        self._crash_tasks.add(task)
        task.add_done_callback(self._crash_tasks.discard)

    async def _handle_crash(self, ctx: Context, exc: BaseException) -> None:
        async with self._lock:
            record = self._records.get(ctx.plugin_name)
            if record is None or record.ctx is not ctx:
                return  # stale: the activation that owned the task is already gone
            await self._deactivate(record, PluginState.FAILED, f"task crashed: {exc!r}")

    # ---------------------------------------------------------------- utils
    def _get(self, name: str) -> _Record:
        try:
            return self._records[name]
        except KeyError:
            raise KernelError(f"unknown plugin {name!r}") from None

    def _info(self, r: _Record) -> PluginInfo:
        return PluginInfo(
            name=r.name,
            state=r.state,
            provides=r.cls.provides,
            inject=dict(r.cls.inject),
            waiting_for=self._missing(r) if r.state is PluginState.PENDING else (),
            error=r.error,
            description=r.cls.description,
        )

    async def _set_state(
        self, record: _Record, state: PluginState, error: str | None = None
    ) -> None:
        old = record.state
        record.state = state
        record.error = error
        if state is PluginState.FAILED:
            self.log.error("plugin %r FAILED: %s", record.name, error)
        else:
            self.log.debug("plugin %r: %s -> %s", record.name, old, state)
        await self._emit_state(record, old)

    async def _emit_state(self, record: _Record, old: PluginState | None) -> None:
        await self.bus.emit(
            PLUGIN_STATE_EVENT,
            {"plugin": record.name, "old": old, "new": record.state, "error": record.error},
        )

    def __repr__(self) -> str:
        states = ", ".join(f"{r.name}={r.state}" for r in self._records.values())
        return f"Kernel({states})"
