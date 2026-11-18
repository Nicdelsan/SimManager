"""Display: user Python scripts run on the cluster that make images of a run (ADR 0008).

Web-only (`WEB_ONLY_PLUGINS`): auto-refresh schedules run while `simman serve` is up, never
during a short CLI command.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from simmanager.contracts import display as contract
from simmanager.contracts import storage as storage_contract
from simmanager.kernel import Context, Plugin

from .config import DisplayConfig
from .service import DURABLE_EVENT_KINDS, DisplayManager


class DisplayPlugin(Plugin):
    name = "display"
    provides = contract.MANAGER_SERVICE_KEY
    contract_version = contract.MANAGER_CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        storage_contract.SERVICE_KEY: storage_contract.CONTRACT_VERSION,
        contract.SERVICE_KEY: contract.CONTRACT_VERSION,
    }
    config_schema = DisplayConfig
    description = "Runs library Python scripts on run folders and keeps the images they make."

    async def apply(self, ctx: Context) -> DisplayManager:
        assert isinstance(ctx.config, DisplayConfig)
        store = ctx.services.require(contract.SERVICE_KEY)
        interrupted = await store.interrupt_running()
        if interrupted:
            ctx.log.warning("%d display execution(s) interrupted by a restart", interrupted)
        manager = DisplayManager(
            storage=ctx.services.require(storage_contract.SERVICE_KEY),
            store=store,
            services=ctx.services,
            config=ctx.config,
            emit=ctx.emit,
            log=ctx.log,
        )
        ctx.task(manager.schedule_loop(), name="schedules")
        ctx.add_disposer(manager.close, "stop running scripts")
        return manager


__all__ = ["DURABLE_EVENT_KINDS", "DisplayConfig", "DisplayManager", "DisplayPlugin"]
