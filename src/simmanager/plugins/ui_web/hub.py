"""Dashboard WebSocket hub: connected browsers, broadcast and message dispatch.

Each client has a bounded outgoing queue drained by its own writer task, so one slow
browser never delays the others; a client whose queue overflows is disconnected (it
reloads and gets a fresh state).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from simmanager.contracts.ui import ClientConnectedHandler, Message, MessageHandler

QUEUE_SIZE = 2000

ClientHandler = Callable[["Client", Message], Awaitable[None]]
"""Internal handler that also needs to know which browser sent the message."""


class Client:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.state: dict[str, Any] = {}
        """Per-browser state owned by other hub components (e.g. live subscriptions)."""
        self._queue: asyncio.Queue[Message | None] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.closed = False

    def send(self, message: Message) -> None:
        """Queue a message; disconnects the client if it cannot keep up."""
        if self.closed:
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)

    async def _write_loop(self) -> None:
        while (message := await self._queue.get()) is not None:
            await self.ws.send_text(json.dumps(message, separators=(",", ":"), default=str))
        with contextlib.suppress(Exception):
            await self.ws.close(code=1008)


class Hub:
    def __init__(self, log: logging.Logger) -> None:
        self._log = log
        self._clients: set[Client] = set()
        self._handlers: dict[str, list[ClientHandler]] = {}
        self._on_connect: list[ClientConnectedHandler] = []
        self._on_disconnect: list[Callable[[Client], None]] = []

    # ------------------------------------------------------------ registration
    def client_count(self) -> int:
        return len(self._clients)

    def clients(self) -> list[Client]:
        return list(self._clients)

    def on_client_message(self, type_: str, handler: ClientHandler) -> Callable[[], None]:
        self._handlers.setdefault(type_, []).append(handler)
        return lambda: _discard(self._handlers.get(type_, []), handler)

    def on_message(self, type_: str, handler: MessageHandler) -> Callable[[], None]:
        async def adapter(client: Client, message: Message) -> None:
            await handler(message)

        return self.on_client_message(type_, adapter)

    def on_client_connected(self, handler: ClientConnectedHandler) -> Callable[[], None]:
        self._on_connect.append(handler)
        return lambda: _discard(self._on_connect, handler)

    def on_client_disconnected(self, handler: Callable[[Client], None]) -> Callable[[], None]:
        self._on_disconnect.append(handler)
        return lambda: _discard(self._on_disconnect, handler)

    # ------------------------------------------------------------------ output
    async def broadcast(self, message: Message) -> None:
        for client in list(self._clients):
            client.send(message)

    # ------------------------------------------------------------- connection
    async def serve(self, ws: WebSocket) -> None:
        await ws.accept()
        client = Client(ws)
        self._clients.add(client)
        writer = asyncio.create_task(client._write_loop(), name="ui-web:ws-writer")
        try:
            client.send({"type": "hello"})
            for on_connect in list(self._on_connect):
                for message in await on_connect():
                    client.send(message)
            while not client.closed and not writer.done():
                try:
                    raw = await ws.receive_text()
                except WebSocketDisconnect:
                    break
                await self._dispatch(client, raw)
        finally:
            self._clients.discard(client)
            client.close()
            for on_disconnect in list(self._on_disconnect):
                on_disconnect(client)
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer

    async def _dispatch(self, client: Client, raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            return
        for handler in list(self._handlers.get(message["type"], [])):
            try:
                await handler(client, message)
            except Exception as exc:
                # Never log the message: it may carry a password or an OTP code.
                self._log.error("handler for %r failed: %s", message["type"], type(exc).__name__)


def _discard(items: list[Any], item: Any) -> None:
    with contextlib.suppress(ValueError):
        items.remove(item)
