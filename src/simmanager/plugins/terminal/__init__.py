"""`terminal` service (M4): interactive shells for the user on the clusters' SSH connections.

Only `simman serve` enables it (`WEB_ONLY_PLUGINS`): the browser is the only client.
All sessions end when the plugin stops (SimManager closes): use tmux on the cluster for
work that must survive (ADR 0007).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import terminal as contract
from simmanager.contracts import transport as transport_contract
from simmanager.kernel import Context, Plugin

from .manager import RingBuffer, TerminalManager, tmux_prefix


class TerminalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orphan_timeout_s: float = Field(1800.0, ge=10, le=7 * 86400)
    """A session nobody views (tab closed or reloaded) is closed after this."""
    buffer_bytes: int = Field(1 << 20, ge=4096, le=64 << 20)
    """Recent output kept per session and replayed when a tab re-attaches."""


class TerminalPlugin(Plugin):
    name = "terminal"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        "storage": 1,
        transport_contract.SERVICE_KEY: transport_contract.CONTRACT_VERSION,
    }
    config_schema = TerminalConfig
    description = "Interactive SSH terminals for the user (web UI only)."

    def apply(self, ctx: Context) -> TerminalManager:
        assert isinstance(ctx.config, TerminalConfig)
        transport = ctx.services.require(transport_contract.SERVICE_KEY)
        manager = TerminalManager(
            lambda: transport,
            ctx.services.require("storage"),
            ctx.emit,
            ctx.log,
            orphan_timeout_s=ctx.config.orphan_timeout_s,
            buffer_bytes=ctx.config.buffer_bytes,
        )

        async def close_all() -> None:
            if count := manager.open_count():
                ctx.log.info("closing %d terminal(s)", count)
            await manager.close_all()

        async def sweeper() -> None:
            interval = min(60.0, manager.orphan_timeout_s / 4)
            while True:
                await asyncio.sleep(interval)
                await manager.sweep()

        ctx.task(sweeper(), name="orphan-sweeper")
        ctx.add_disposer(close_all, "close terminal sessions")
        return manager


__all__ = ["RingBuffer", "TerminalConfig", "TerminalManager", "TerminalPlugin", "tmux_prefix"]
