"""Display endpoints (ADR 0008) on top of the `display.manager` service.

GET  /api/display/scripts                     script library (latest version of each)
POST /api/display/scripts                     {name, body, setup, interpreter, message}
                                              -> new version when the content changed
GET  /api/display/scripts/{id}/versions       history, newest first
GET  /api/display/diff?old=&new=              unified diff of two versions
GET  /api/runs/{id}/display                   executions (with images), schedules, busy
POST /api/runs/{id}/display/run               {script_id}: run the latest version now (202)
POST /api/runs/{id}/display/schedule          {script_id, interval_s, enabled}: auto-refresh
GET  /api/runs/{id}/display/files?path=       list a folder below the run folder
GET  /api/runs/{id}/display/file?path=&force= an image below the run folder (bytes)
GET  /api/display/images/{id}                 a stored image of the history (bytes)
POST /api/display/images/{id}/fetch           copy an image skipped as too large

Pressing "Run" or enabling auto-refresh is the user's approval; the transport records
every execution as `action.executed` / `action.result`.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import display as contract
from simmanager.contracts.display import (
    DisplayError,
    DisplayService,
    ExecutionRecord,
    ExecutionTrigger,
    ImageRecord,
    ImageTooLargeError,
    RemoteEntry,
    ScheduleRecord,
    ScriptDraft,
    ScriptVersion,
)
from simmanager.contracts.transport import ClusterUnavailableError, TransportError

from .api import Api, _iso

APPROVED_BY = "web"


class ScriptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=1_000_000)
    setup: str = Field("", max_length=20_000)
    interpreter: str = Field("python3", min_length=1, max_length=500)
    message: str = Field("", max_length=500)


class RunScriptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_id: int = Field(ge=1)


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_id: int = Field(ge=1)
    interval_s: int = Field(ge=1, le=7 * 86_400)
    enabled: bool


def version_json(v: ScriptVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "script_id": v.script_id,
        "number": v.number,
        "sha256": v.sha256,
        "body": v.body,
        "setup": v.setup,
        "interpreter": v.interpreter,
        "message": v.message,
        "created_at": _iso(v.created_at),
    }


def image_json(i: ImageRecord) -> dict[str, Any]:
    return {
        "id": i.id,
        "execution_id": i.execution_id,
        "path": i.path,
        "size": i.size,
        "mtime": i.mtime,
        "media_type": i.media_type,
        "stored": i.sha256 is not None,
        "skipped": str(i.skipped) if i.skipped else None,
        "detail": i.detail,
    }


def execution_json(e: ExecutionRecord) -> dict[str, Any]:
    return {
        "id": e.id,
        "run_id": e.run_id,
        "script_id": e.script_id,
        "script_name": e.script_name,
        "version_id": e.version_id,
        "version_number": e.version_number,
        "trigger": str(e.trigger),
        "status": str(e.status),
        "started_at": _iso(e.started_at),
        "finished_at": _iso(e.finished_at),
        "exit_status": e.exit_status,
        "stdout": e.stdout_tail,
        "stderr": e.stderr_tail,
        "error": e.error,
        "images": [image_json(i) for i in e.images],
    }


def schedule_json(s: ScheduleRecord) -> dict[str, Any]:
    return {
        "run_id": s.run_id,
        "script_id": s.script_id,
        "interval_s": s.interval_s,
        "enabled": s.enabled,
        "approved_at": _iso(s.approved_at),
        "last_run_at": _iso(s.last_run_at),
    }


def entry_json(e: RemoteEntry) -> dict[str, Any]:
    return {
        "name": e.name,
        "path": e.path,
        "is_dir": e.is_dir,
        "size": e.size,
        "mtime": e.mtime,
        "media_type": e.media_type,
    }


def _image_response(media_type: str, data: bytes) -> Response:
    return Response(data, media_type=media_type, headers={"Content-Disposition": "inline"})


class _Errors:
    """Map service/transport errors to HTTP statuses."""

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if isinstance(exc, ImageTooLargeError):
            raise HTTPException(
                413, {"message": str(exc), "size": exc.size, "limit": exc.limit}
            ) from None
        if isinstance(exc, DisplayError):
            raise HTTPException(422, str(exc)) from None
        if isinstance(exc, ClusterUnavailableError):
            raise HTTPException(409, f"{exc.cluster_id} is {exc.state}: {exc.detail}") from None
        if isinstance(exc, TransportError):
            raise HTTPException(502, str(exc)) from None


def build_display_router(api: Api) -> APIRouter:
    r = APIRouter()

    def service() -> DisplayService:
        return api._need(contract.MANAGER_SERVICE_KEY)  # type: ignore[no-any-return]

    async def need_run(run_id: int) -> None:
        if await api.storage.run(run_id) is None:
            raise HTTPException(404, "unknown run")

    # ------------------------------------------------------------- library
    @r.get("/display/scripts")
    async def scripts() -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "name": s.name,
                "created_at": _iso(s.created_at),
                "latest": version_json(s.latest),
            }
            for s in await service().scripts()
        ]

    @r.post("/display/scripts")
    async def save_script(body: ScriptBody) -> dict[str, Any]:
        draft = ScriptDraft(
            name=body.name,
            body=body.body,
            setup=body.setup,
            interpreter=body.interpreter,
            message=body.message,
        )
        with _Errors():
            version, created = await service().save_script(draft)
        return {"created": created, "version": version_json(version)}

    @r.get("/display/scripts/{script_id}/versions")
    async def versions(script_id: int) -> list[dict[str, Any]]:
        return [version_json(v) for v in await service().versions(script_id)]

    @r.get("/display/diff")
    async def diff(old: int = Query(ge=1), new: int = Query(ge=1)) -> dict[str, Any]:
        with _Errors():
            text = await service().diff(old, new)
        return {"diff": text}

    # ----------------------------------------------------------- run panel
    @r.get("/runs/{run_id}/display")
    async def run_display(run_id: int, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        await need_run(run_id)
        svc = service()
        return {
            "busy": svc.busy(run_id),
            "max_image_bytes": svc.max_image_bytes,
            "executions": [execution_json(e) for e in await svc.executions(run_id, limit=limit)],
            "schedules": [schedule_json(s) for s in await svc.schedules(run_id)],
        }

    @r.post("/runs/{run_id}/display/run", status_code=202)
    async def run_script(run_id: int, body: RunScriptBody) -> dict[str, Any]:
        await need_run(run_id)
        svc = service()
        if svc.busy(run_id):
            raise HTTPException(409, "a script is already running on this run")
        with _Errors():
            execution = await svc.start(
                run_id, body.script_id, trigger=ExecutionTrigger.MANUAL, approved_by=APPROVED_BY
            )
        return execution_json(execution)

    @r.post("/runs/{run_id}/display/schedule")
    async def schedule(run_id: int, body: ScheduleBody) -> dict[str, Any]:
        await need_run(run_id)
        with _Errors():
            record = await service().set_schedule(
                run_id,
                body.script_id,
                interval_s=body.interval_s,
                enabled=body.enabled,
                approved_by=APPROVED_BY,
            )
        return schedule_json(record)

    @r.get("/runs/{run_id}/display/files")
    async def files(run_id: int, path: str = "") -> list[dict[str, Any]]:
        await need_run(run_id)
        with _Errors():
            entries = await service().browse(run_id, path)
        return [entry_json(e) for e in entries]

    @r.get("/runs/{run_id}/display/file")
    async def file(run_id: int, path: str, force: bool = False) -> Response:
        await need_run(run_id)
        with _Errors():
            media_type, data = await service().read_image(run_id, path, force=force)
        return _image_response(media_type, data)

    # --------------------------------------------------------------- images
    @r.get("/display/images/{image_id}")
    async def image(image_id: int) -> Response:
        found = await service().image_data(image_id)
        if found is None:
            raise HTTPException(404, "image not stored")
        record, data = found
        return _image_response(record.media_type, data)

    @r.post("/display/images/{image_id}/fetch")
    async def fetch(image_id: int) -> dict[str, Any]:
        with _Errors():
            record = await service().fetch_skipped(image_id)
        return image_json(record)

    return r
