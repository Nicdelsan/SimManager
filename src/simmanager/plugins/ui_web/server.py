"""uvicorn running inside the kernel's event loop, and the `ui.web` service."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import sys
from collections.abc import Callable, Generator

import uvicorn

from simmanager.contracts.ui import ClientConnectedHandler, Message, MessageHandler

from .hub import Hub
from .security import LOOPBACK, TOKEN_PARAM, SecurityPolicy

STARTUP_TIMEOUT_S = 10.0
UVICORN_LOGGER = "uvicorn.error"
SHUTDOWN_TIMEOUT_S = 5.0


class UiWebError(RuntimeError):
    pass


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        # Ctrl+C belongs to the process owner (`simman serve` / asyncio.run), which stops
        # the kernel and therefore this server. uvicorn must not steal the signal.
        yield


def bind_loopback(port: int) -> socket.socket:
    """Bind 127.0.0.1:<port> (0 = any free port). Never another interface."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        # POSIX: allow restart while old connections are in TIME_WAIT. (On Windows the
        # same option would allow two servers on one port, so it stays off there.)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((LOOPBACK, port))
    except OSError as exc:
        sock.close()
        raise UiWebError(
            f"cannot listen on {LOOPBACK}:{port} ({exc.strerror or exc}); "
            "is another `simman serve` running? Use --port to choose another port."
        ) from exc
    sock.set_inheritable(False)
    return sock


class UiWebServer:
    """Implements `simmanager.contracts.ui.UiWeb`."""

    def __init__(self, hub: Hub, policy: SecurityPolicy) -> None:
        self.hub = hub
        self.policy = policy
        self._server: _Server | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- contract
    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK}:{self.policy.port}/"

    @property
    def login_url(self) -> str:
        """URL with the session token: show it to the user, never log it."""
        return f"{self.base_url}?{TOKEN_PARAM}={self.policy.token}"

    def client_count(self) -> int:
        return self.hub.client_count()

    async def broadcast(self, message: Message) -> None:
        await self.hub.broadcast(message)

    def on_message(self, type_: str, handler: MessageHandler) -> Callable[[], None]:
        return self.hub.on_message(type_, handler)

    def on_client_connected(self, handler: ClientConnectedHandler) -> Callable[[], None]:
        return self.hub.on_client_connected(handler)

    # ------------------------------------------------------------ lifecycle
    async def start(self, app: object, sock: socket.socket) -> asyncio.Task[None]:
        # uvicorn's WebSocket implementation logs every frame at DEBUG on this logger,
        # including password/OTP answers: never let it go below INFO.
        uv_log = logging.getLogger(UVICORN_LOGGER)
        if uv_log.level < logging.INFO:
            uv_log.setLevel(logging.INFO)
        config = uvicorn.Config(
            app,  # type: ignore[arg-type]
            lifespan="off",
            log_config=None,
            access_log=False,  # the login URL carries the token
            ws="websockets-sansio",
            timeout_graceful_shutdown=int(SHUTDOWN_TIMEOUT_S),
        )
        self._server = server = _Server(config)
        self._task = task = asyncio.create_task(server.serve(sockets=[sock]), name="ui-web:server")
        async with asyncio.timeout(STARTUP_TIMEOUT_S):
            while not server.started and not task.done():  # noqa: ASYNC110 (uvicorn has no event)
                await asyncio.sleep(0.01)
        if task.done():
            task.result()  # re-raise the startup error
            raise UiWebError("web server stopped during startup")
        return task

    async def stop(self) -> None:
        server, task = self._server, self._task
        if server is None or task is None:
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), SHUTDOWN_TIMEOUT_S + 2)
        except (TimeoutError, Exception):
            server.force_exit = True
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
