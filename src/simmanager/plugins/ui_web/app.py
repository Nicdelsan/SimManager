"""FastAPI application factory, wrapped in the security middleware."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp

from .hub import Hub
from .security import SecurityMiddleware, SecurityPolicy

STATIC_DIR = Path(__file__).parent / "static"


def create_app(policy: SecurityPolicy, hub: Hub, routers: Iterable[APIRouter] = ()) -> ASGIApp:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await hub.serve(websocket)

    for router in routers:
        app.include_router(router, prefix="/api")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return SecurityMiddleware(app, policy)
