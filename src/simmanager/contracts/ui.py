"""`ui.web` contract: the local web UI server, as seen by other plugins.

Scope: the shared dashboard WebSocket (live data and authentication prompts). Plugins
that need their own channel per session (e.g. the M4 terminal) will get dedicated,
authenticated WebSocket endpoints through an additive extension of this contract; they
are not expected to multiplex over `broadcast`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

SERVICE_KEY = "ui.web"
CONTRACT_VERSION = 1

Message = Mapping[str, Any]
"""A JSON object; every message has a string `type`."""

MessageHandler = Callable[[Message], Awaitable[None]]
ClientConnectedHandler = Callable[[], Awaitable[list[Message]]]
"""Returns the messages a newly connected browser must receive (e.g. pending prompts)."""


class UiWeb(Protocol):
    @property
    def base_url(self) -> str:
        """`http://127.0.0.1:<port>/` without credentials (safe to log)."""
        ...

    def client_count(self) -> int:
        """Browsers currently connected to the dashboard WebSocket."""
        ...

    async def broadcast(self, message: Message) -> None:
        """Send a message to every connected dashboard WebSocket."""
        ...

    def on_message(self, type_: str, handler: MessageHandler) -> Callable[[], None]:
        """Handle browser -> server messages of `type_`. Returns an unsubscribe function."""
        ...

    def on_client_connected(self, handler: ClientConnectedHandler) -> Callable[[], None]:
        """Run when a dashboard WebSocket connects. Returns an unsubscribe function."""
        ...
