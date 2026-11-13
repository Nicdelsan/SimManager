"""Helpers for tests against a real uvicorn started by the ui-web plugin (port 0).

Starlette's TestClient runs the app in another event loop, which does not work with the
kernel's services (aiosqlite, asyncssh): the tests use httpx.AsyncClient and raw sockets
against the real server in the test's own loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from simmanager.contracts import ui as ui_contract
from simmanager.kernel import Kernel, Plugin
from simmanager.plugins.ui_web import UiWebPlugin, UiWebServer

# httpx builds an SSL context per client (~1 s on Windows); the server is plain HTTP.
_SSL = ssl.create_default_context()


@dataclass
class Web:
    kernel: Kernel
    server: UiWebServer

    @property
    def port(self) -> int:
        return self.server.policy.port

    @property
    def base(self) -> str:
        return self.server.base_url.rstrip("/")

    @property
    def origin(self) -> str:
        return self.base

    @property
    def cookie(self) -> str:
        return f"{self.server.policy.cookie_name}={self.server.policy.token}"

    def client(self, *, authenticated: bool = True) -> httpx.AsyncClient:
        headers = {"Cookie": self.cookie} if authenticated else {}
        return httpx.AsyncClient(base_url=self.base, headers=headers, timeout=10, verify=_SSL)

    async def post_json(self, path: str, body: Any = None) -> httpx.Response:
        async with self.client() as c:
            return await c.post(path, json=body or {}, headers={"Origin": self.origin})


@asynccontextmanager
async def running_web(
    *plugins: type[Plugin], config: dict[str, dict[str, Any]] | None = None
) -> AsyncIterator[Web]:
    cfg = {"ui-web": {"port": 0}, **(config or {})}
    kernel = Kernel(cfg)
    try:
        await kernel.add(*plugins, UiWebPlugin)
        server = kernel.services.require(ui_contract.SERVICE_KEY)
        assert isinstance(server, UiWebServer)
        yield Web(kernel, server)
    finally:
        await kernel.stop()


async def ws_handshake_status(
    port: int, *, host: str | None = None, headers: Sequence[tuple[str, str]] = ()
) -> int:
    """Send a raw WebSocket upgrade to /ws and return the HTTP status of the answer."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            "GET /ws HTTP/1.1",
            f"Host: {host or f'127.0.0.1:{port}'}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            *(f"{k}: {v}" for k, v in headers),
        ]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), 5)
        return int(status_line.split()[1])
    finally:
        writer.close()


def ws_connect(web: Web) -> Any:
    """Authenticated dashboard WebSocket (async context manager)."""
    from websockets.asyncio.client import connect

    return connect(
        f"ws://127.0.0.1:{web.port}/ws",
        origin=web.origin,
        additional_headers={"Cookie": web.cookie},
        open_timeout=5,
    )


async def recv_json(ws: Any, timeout_s: float = 5.0) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout_s))  # type: ignore[no-any-return]


async def recv_type(ws: Any, type_: str, timeout_s: float = 5.0) -> dict[str, Any]:
    """Next message of `type_`, skipping others."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"no {type_!r} message within {timeout_s} s")
        message = await recv_json(ws, remaining)
        if message.get("type") == type_:
            return message
