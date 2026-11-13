"""`notifier` contracts: notification providers (`notifier.<kind>`) and routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

SERVICE_KEY = "notifier.router"
CONTRACT_VERSION = 1

PROVIDER_PREFIX = "notifier."
PROVIDER_CONTRACT_VERSION = 1


class NotificationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class NotificationMessage:
    """User-facing notification payload (must never contain secrets)."""

    title: str
    body: str
    severity: NotificationSeverity = NotificationSeverity.INFO
    category: str = "general"
    cluster_id: str | None = None
    run_id: int | None = None
    alert_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class NotifierProvider(Protocol):
    """One delivery backend (e.g. `notifier.desktop`, future `notifier.telegram`)."""

    @property
    def kind(self) -> str: ...

    def available(self) -> bool: ...

    async def send(self, message: NotificationMessage) -> bool: ...


class NotificationService(Protocol):
    """Routes events/alerts through batching, quiet hours, and rate limiting."""

    def providers(self) -> list[str]: ...

    async def notify(self, message: NotificationMessage) -> bool:
        """Queue/deliver `message` according to routing, batching, quiet hours, and rate limits.

        Returns True when delivered immediately or queued for batched delivery; False when
        suppressed (quiet hours / rate limit / no providers).
        """
        ...

    async def send_test(
        self,
        title: str = "SimManager test notification",
        body: str = "Notifications are working.",
    ) -> dict[str, bool]:
        """Send a test notification directly to every available provider."""
        ...
