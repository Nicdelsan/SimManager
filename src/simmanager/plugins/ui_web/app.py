"""FastAPI application factory, wrapped in the security middleware."""

from __future__ import annotations

import mimetypes
from collections.abc import Iterable
from pathlib import Path

from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp

from .hub import Hub
from .security import SecurityMiddleware, SecurityPolicy

STATIC_DIR = Path(__file__).parent / "static"

# The Windows registry may map .js to text/plain; with `nosniff` the browser would then
# refuse to run the ES modules. Register the right types explicitly.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def create_app(
    policy: SecurityPolicy,
    hub: Hub,
    routers: Iterable[APIRouter] = (),
    ws_routers: Iterable[APIRouter] = (),
) -> ASGIApp:
    """`routers` are mounted under /api; `ws_routers` at the root (e.g. /ws/term/...)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/terminal", include_in_schema=False)
    async def terminal_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "terminal.html")

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await hub.serve(websocket)

    for router in routers:
        app.include_router(router, prefix="/api")
    for router in ws_routers:
        app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return SecurityMiddleware(app, policy)
