"""`notifier-desktop` plugin: native Windows/Linux desktop notifications via `desktop-notifier`."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict

from simmanager.contracts import notifier as contract
from simmanager.contracts.notifier import NotificationMessage, NotificationSeverity
from simmanager.kernel import Context, Plugin

log = logging.getLogger("simmanager.plugin.notifier_desktop")


class DesktopNotifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_name: str = "SimManager"


class DesktopNotifierProvider:
    """Implements `simmanager.contracts.notifier.NotifierProvider` for desktop OS toasts."""

    def __init__(self, app_name: str = "SimManager") -> None:
        self._app_name = app_name
        self._client: Any = None
        self._unavailable_reason: str | None = None

    @property
    def kind(self) -> str:
        return "desktop"

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._unavailable_reason is not None:
            return None
        try:
            from desktop_notifier import DesktopNotifier

            self._client = DesktopNotifier(app_name=self._app_name)
        except Exception as exc:
            self._unavailable_reason = f"{type(exc).__name__}: {exc}"
            log.warning("desktop notifications unavailable: %s", self._unavailable_reason)
            return None
        return self._client

    def available(self) -> bool:
        if sys.platform.startswith("linux") and not (
            os.environ.get("DBUS_SESSION_BUS_ADDRESS")
            or os.environ.get("DISPLAY")
            or os.environ.get("WAYLAND_DISPLAY")
        ):
            return False
        return self._ensure_client() is not None

    async def send(self, message: NotificationMessage) -> bool:
        client = self._ensure_client()
        if client is None:
            return False
        try:
            from desktop_notifier import Urgency

            urgency = {
                NotificationSeverity.CRITICAL: Urgency.Critical,
                NotificationSeverity.WARNING: Urgency.Normal,
                NotificationSeverity.INFO: Urgency.Low,
            }[message.severity]
            await client.send(
                title=message.title,
                message=message.body,
                urgency=urgency,
            )
            return True
        except Exception as exc:
            log.warning("desktop notification failed (falling back silently): %s", exc)
            return False


class DesktopNotifierPlugin(Plugin):
    name = "notifier-desktop"
    provides = f"{contract.PROVIDER_PREFIX}desktop"
    contract_version = contract.PROVIDER_CONTRACT_VERSION
    config_schema = DesktopNotifierConfig
    description = "Native desktop notifications (Windows WinRT / Linux D-Bus)."

    def apply(self, ctx: Context) -> DesktopNotifierProvider:
        assert isinstance(ctx.config, DesktopNotifierConfig)
        provider = DesktopNotifierProvider(app_name=ctx.config.app_name)
        if not provider.available():
            ctx.log.warning(
                "desktop notification backend not available; notifications will fall back silently"
            )
        return provider
