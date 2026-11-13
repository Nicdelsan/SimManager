"""Local web UI (M3): FastAPI + uvicorn in the kernel's event loop.

Only `simman serve` enables it (see `WEB_ONLY_PLUGINS`). Security: `security.py`, ADR 0005.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import ui as contract
from simmanager.kernel import Context, Plugin

from .app import create_app
from .hub import Hub
from .security import SecurityPolicy, new_token
from .server import UiWebError, UiWebServer, bind_loopback


class UiWebConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int = Field(8765, ge=0, le=65535)
    """TCP port on 127.0.0.1 (0 = any free port). The address is not configurable."""
    open_browser: bool = True
    """`simman serve` opens the browser on the login URL."""


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
        try:
            task = await server.start(create_app(policy, hub), sock)
        except BaseException:
            sock.close()
            raise

        async def watch() -> None:
            await task  # an unexpected crash marks the plugin FAILED

        ctx.task(watch(), name="server")
        ctx.add_disposer(server.stop, "stop web server")  # runs before the task is cancelled
        ctx.log.info("web UI listening on %s", server.base_url)
        return server


__all__ = ["UiWebConfig", "UiWebError", "UiWebPlugin", "UiWebServer"]
