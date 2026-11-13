"""Local web UI (M3): FastAPI + uvicorn in the kernel's event loop.

Only `simman serve` enables it (see `WEB_ONLY_PLUGINS`). Security: `security.py`, ADR 0005.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import auth as auth_contract
from simmanager.contracts import monitor as monitor_contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts import terminal as terminal_contract
from simmanager.contracts import transport as transport_contract
from simmanager.contracts import ui as contract
from simmanager.contracts.monitor import PollReport
from simmanager.contracts.storage import EventRecord
from simmanager.kernel import Context, Plugin

from .actions import build_actions_router
from .api import Api, ClusterStatusCache, build_router, event_json
from .app import create_app
from .campaigns import build_campaign_router
from .hub import Hub
from .live import Live, RateLimit
from .prompter import WebPrompter
from .security import SecurityPolicy, new_token
from .server import UiWebError, UiWebServer, bind_loopback
from .terminal import TerminalApi, build_terminal_router, build_terminal_ws_router, terminal_json


class UiWebConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int = Field(8765, ge=0, le=65535)
    """TCP port on 127.0.0.1 (0 = any free port). The address is not configurable."""
    open_browser: bool = True
    """`simman serve` opens the browser on the login URL."""
    live_interval_s: float = Field(1.0, ge=0.05, le=60.0)
    """Minimum seconds between live updates of one run (plots, backfill progress)."""


class UiWebPlugin(Plugin):
    name = "ui-web"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    config_schema = UiWebConfig
    description = "Local web UI on 127.0.0.1 (dashboard, plots, authentication prompts)."

    async def apply(self, ctx: Context) -> UiWebServer:
        assert isinstance(ctx.config, UiWebConfig)
        sock = bind_loopback(ctx.config.port)
        policy = SecurityPolicy(token=new_token(), port=sock.getsockname()[1])
        hub = Hub(ctx.log)
        server = UiWebServer(hub, policy)
        status = ClusterStatusCache()
        api = Api(ctx.services, status)
        ctx.add_disposer(api.close, "cancel API background tasks")

        async def on_transport_state(payload: dict[str, Any]) -> None:
            await hub.broadcast(status.on_transport_state(payload))

        async def on_polled(report: PollReport) -> None:
            await hub.broadcast(status.on_polled(report))

        ctx.on(transport_contract.STATE_EVENT, on_transport_state)
        ctx.on(monitor_contract.POLLED_EVENT, on_polled)

        live = Live(hub, ctx.services, ctx.log, interval_s=ctx.config.live_interval_s)
        ctx.add_disposer(live.close, "cancel live flushes")
        for unsubscribe in live.install():
            ctx.add_disposer(unsubscribe, "hub handler")
        backfill_limit = RateLimit(ctx.config.live_interval_s, time.monotonic)

        async def on_event_appended(record: EventRecord) -> None:
            await hub.broadcast({"type": "event", **event_json(record)})

        async def on_run_progress(payload: dict[str, Any]) -> None:
            await hub.broadcast({"type": "run.progress", **payload})

        async def on_backfill(payload: dict[str, Any]) -> None:
            key = (payload.get("run_id"), payload.get("file_path"))
            if payload.get("done") or backfill_limit.allow(key):
                await hub.broadcast({"type": "backfill.progress", **payload})

        async def on_series_updated(payload: dict[str, Any]) -> None:
            run_id = payload.get("run_id")
            if type(run_id) is int:
                live.series_updated(run_id)

        async def on_terminal_state(info: terminal_contract.TerminalInfo) -> None:
            await hub.broadcast({"type": "terminal.state", **terminal_json(info)})

        ctx.on(storage_contract.EVENT_APPENDED, on_event_appended)
        ctx.on(terminal_contract.STATE_EVENT, on_terminal_state)
        ctx.on("run.progress", on_run_progress)
        ctx.on("backfill.progress", on_backfill)
        ctx.on("series.updated", on_series_updated)
        try:
            terminals = TerminalApi(api)
            app = create_app(
                policy,
                hub,
                [
                    build_router(api),
                    build_campaign_router(api),
                    build_terminal_router(terminals),
                    build_actions_router(api),
                ],
                [build_terminal_ws_router(terminals)],
            )
            task = await server.start(app, sock)
        except BaseException:
            sock.close()
            raise

        async def watch() -> None:
            await task  # an unexpected crash marks the plugin FAILED

        ctx.task(watch(), name="server")
        ctx.add_disposer(server.stop, "stop web server")  # runs before the task is cancelled
        ctx.log.info("web UI listening on %s", server.base_url)
        return server


class WebPrompterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_timeout_s: float = Field(300.0, gt=0, le=3600)
    """Unanswered prompts end after this; the cluster then waits for "Reconnect"."""


class WebPrompterPlugin(Plugin):
    name = "prompter-web"
    provides = auth_contract.SERVICE_KEY
    contract_version = auth_contract.CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {contract.SERVICE_KEY: contract.CONTRACT_VERSION}
    config_schema = WebPrompterConfig
    description = "Asks passwords, OTP codes and confirmations in the web UI."

    def apply(self, ctx: Context) -> WebPrompter:
        assert isinstance(ctx.config, WebPrompterConfig)
        prompter = WebPrompter(
            ctx.services.require(contract.SERVICE_KEY),
            timeout_s=ctx.config.prompt_timeout_s,
            log=ctx.log,
        )
        for unsubscribe in prompter.install():
            ctx.add_disposer(unsubscribe, "ui handler")
        ctx.add_disposer(prompter.close, "end pending prompts")
        return prompter


__all__ = [
    "UiWebConfig",
    "UiWebError",
    "UiWebPlugin",
    "UiWebServer",
    "WebPrompter",
    "WebPrompterConfig",
    "WebPrompterPlugin",
]
