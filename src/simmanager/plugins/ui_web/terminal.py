"""Terminal endpoints (M4) on top of the `terminal` service.

GET  /api/terminals                      sessions
POST /api/terminals                      {cluster_id, cwd?, run_id?, cols, rows, tmux?,
                                          attach_tmux?} -> {session, ticket}
POST /api/terminals/{id}/ticket          new ticket (re-attach after a page reload)
POST /api/terminals/{id}/close           end the session
GET  /api/clusters/{id}/tmux             live `simman-*` tmux sessions (re-attach menu)
GET  /api/runs/{id}/quick-actions        commands to insert into the run's terminal
WS   /ws/term/{id}?ticket=...            the terminal itself

On top of the checks of every request (cookie, Host, Origin; ADR 0005) the WebSocket needs
a single-use ticket bound to the session, valid for `TICKET_TTL_S`, issued by a POST.

Frames: binary = terminal bytes (browser -> server: keystrokes, at most
`MAX_INPUT_FRAME` bytes; server -> browser: output). Text = JSON control messages:
browser -> server `{"type": "resize", "cols", "rows"}`; server -> browser
`{"type": "replay"}` (recent output follows; reset the screen first) and
`{"type": "state", ...session}`. Close codes: 1000 session closed, 4001 opened in another
tab, 1008 refused (ticket), 1009 input frame too big, 1011 service unavailable.

Privacy: frame contents are never logged; errors log the exception type only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import posixpath
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.websockets import WebSocketDisconnect

from simmanager.contracts import terminal as terminal_contract
from simmanager.contracts.terminal import (
    TerminalAttachment,
    TerminalInfo,
    TerminalRequest,
    TerminalService,
    TerminalState,
)
from simmanager.contracts.transport import (
    ClusterUnavailableError,
    ConnectionState,
    TransportError,
)

from .api import Api, _iso
from .quick import STDOUT_PARSERS, input_candidates, quick_actions

TICKET_TTL_S = 30.0
MAX_INPUT_FRAME = 64 * 1024
MAX_TICKETS = 256

log = logging.getLogger("simmanager.plugin.ui-web.terminal")


@dataclass
class Tickets:
    """Single-use WebSocket tickets, each bound to one terminal session."""

    ttl_s: float = TICKET_TTL_S
    clock: Callable[[], float] = time.monotonic
    _issued: dict[str, tuple[str, float]] = field(default_factory=dict)

    def issue(self, session_id: str) -> str:
        now = self.clock()
        self._issued = {t: v for t, v in self._issued.items() if v[1] > now}
        while len(self._issued) >= MAX_TICKETS:
            self._issued.pop(next(iter(self._issued)))
        ticket = secrets.token_urlsafe(32)
        self._issued[ticket] = (session_id, now + self.ttl_s)
        return ticket

    def consume(self, session_id: str, ticket: str | None) -> bool:
        if not ticket:
            return False
        entry = self._issued.pop(ticket, None)  # used once, whatever the outcome
        return (
            entry is not None
            and secrets.compare_digest(entry[0], session_id)
            and self.clock() < entry[1]
        )


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str = Field(min_length=1, max_length=200)
    cwd: str | None = Field(None, max_length=4096)
    run_id: int | None = Field(None, ge=1)
    cols: int = Field(80, ge=10, le=1000)
    rows: int = Field(24, ge=4, le=500)
    tmux: bool | None = None
    attach_tmux: str | None = Field(None, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("cwd")
    @classmethod
    def _absolute(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/") or any(ord(c) < 32 for c in value):
            raise ValueError("cwd must be an absolute POSIX path")
        return posixpath.normpath(value)


class ResizeMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    cols: int = Field(ge=10, le=1000)
    rows: int = Field(ge=4, le=500)


def terminal_json(info: TerminalInfo) -> dict[str, Any]:
    return {
        "id": info.id,
        "cluster_id": info.cluster_id,
        "cwd": info.cwd,
        "run_id": info.run_id,
        "title": info.title,
        "state": str(info.state),
        "detail": info.detail,
        "created_at": _iso(info.created_at),
        "attached": info.attached,
        "tmux_session": info.tmux_session,
        "exit_status": info.exit_status,
        "error": str(info.error) if info.error else None,
    }


class TerminalApi:
    def __init__(self, api: Api, tickets: Tickets | None = None) -> None:
        self.api = api
        self.tickets = tickets or Tickets()

    def service(self) -> TerminalService:
        return self.api._need(terminal_contract.SERVICE_KEY)  # type: ignore[no-any-return]

    def session(self, session_id: str) -> TerminalInfo:
        info = self.service().get(session_id)
        if info is None:
            raise HTTPException(404, "unknown terminal session")
        return info


def build_terminal_router(t: TerminalApi) -> APIRouter:
    r = APIRouter()

    @r.get("/terminals")
    async def sessions() -> list[dict[str, Any]]:
        return [terminal_json(i) for i in t.service().sessions()]

    @r.post("/terminals", status_code=201)
    async def open_terminal(body: OpenBody) -> dict[str, Any]:
        service = t.service()
        try:
            info = await service.open(
                TerminalRequest(
                    cluster_id=body.cluster_id,
                    cwd=body.cwd,
                    run_id=body.run_id,
                    cols=body.cols,
                    rows=body.rows,
                    tmux=body.tmux,
                    attach_tmux=body.attach_tmux,
                )
            )
        except KeyError:
            raise HTTPException(404, "unknown cluster") from None
        return {"session": terminal_json(info), "ticket": t.tickets.issue(info.id)}

    @r.post("/terminals/{session_id}/ticket")
    async def ticket(session_id: str) -> dict[str, Any]:
        info = t.session(session_id)
        return {"session": terminal_json(info), "ticket": t.tickets.issue(info.id)}

    @r.post("/terminals/{session_id}/close")
    async def close(session_id: str) -> dict[str, Any]:
        t.session(session_id)
        with contextlib.suppress(KeyError):  # closed meanwhile
            await t.service().close(session_id)
        return {"id": session_id, "state": str(TerminalState.CLOSED)}

    @r.get("/clusters/{cluster_id}/tmux")
    async def tmux_sessions(cluster_id: str) -> list[str]:
        service = t.service()
        if cluster_id not in t.api.transport.clusters():
            raise HTTPException(404, "unknown cluster")
        try:
            return await service.tmux_sessions(cluster_id)
        except ClusterUnavailableError as exc:
            raise HTTPException(409, f"{cluster_id} is {exc.state}: {exc.detail}") from None
        except TransportError as exc:
            raise HTTPException(502, str(exc)) from None

    @r.get("/runs/{run_id}/quick-actions")
    async def run_quick_actions(run_id: int) -> dict[str, Any]:
        api = t.api
        run = await api.storage.run(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        job = await api.storage.job_for_run(run_id)
        solver = await api.ts.get_run_solver_state(run_id)
        work_dir = run.work_dir or (job.info.work_dir if job else None)
        logs = (
            [
                posixpath.join(work_dir, c.file_path)
                for c in await api.ts.list_cursors(run_id)
                if c.parser_kind in STDOUT_PARSERS
            ]
            if work_dir
            else []
        )
        case_dir = (solver.case_dir if solver else None) or work_dir
        inputs, note = await _list_inputs(
            api, run.cluster_id, solver.solver if solver else None, case_dir
        )
        actions = quick_actions(
            work_dir=work_dir,
            job_id=job.info.job_id if job else None,
            logs=sorted(logs),
            inputs=inputs,
        )
        return {
            "run_id": run_id,
            "cluster_id": run.cluster_id,
            "work_dir": work_dir,
            "job": (
                {
                    "job_id": job.info.job_id,
                    "name": job.info.name,
                    "active": job.active,
                    "state": str(job.info.state),
                }
                if job
                else None
            ),
            "actions": [
                {"group": a.group, "label": a.label, "command": a.command} for a in actions
            ],
            "note": note,
        }

    return r


async def _list_inputs(
    api: Api, cluster_id: str | None, solver: str | None, case_dir: str | None
) -> tuple[list[str], str | None]:
    """Input files of the case, listed over SFTP only when the cluster is already
    connected: a page load must never trigger a login or an OTP prompt."""
    if not cluster_id or not case_dir:
        return [], None
    transport = api.transport
    if cluster_id not in transport.clusters():
        return [], None
    if transport.state(cluster_id) is not ConnectionState.CONNECTED:
        return [], "input files are listed once the cluster is connected"
    try:
        session = await transport.session(cluster_id)
        names = [posixpath.basename(e.path) for e in await session.listdir(case_dir)]
        if solver == "openfoam":
            system = posixpath.join(case_dir, "system")
            names = [f"system/{posixpath.basename(e.path)}" for e in await session.listdir(system)]
    except TransportError as exc:
        return [], f"cannot list the case folder: {exc}"
    return input_candidates(solver, case_dir, names), None


def build_terminal_ws_router(t: TerminalApi) -> APIRouter:
    r = APIRouter()

    @r.websocket("/ws/term/{session_id}")
    async def terminal_ws(websocket: WebSocket, session_id: str) -> None:
        # Cookie, Host and Origin were checked by the middleware before this point.
        service = t.api.services.get(terminal_contract.SERVICE_KEY)
        if not t.tickets.consume(session_id, websocket.query_params.get("ticket")):
            await websocket.close(code=1008)
            return
        if service is None:
            await websocket.close(code=1011)
            return
        try:
            attachment = service.attach(session_id)
        except KeyError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            await _serve(websocket, attachment)
        except Exception as exc:  # never log frame contents
            log.warning("terminal %s: WebSocket error (%s)", session_id, type(exc).__name__)
        finally:
            attachment.detach()

    return r


async def _serve(ws: WebSocket, att: TerminalAttachment) -> None:
    await ws.send_text(json.dumps({"type": "replay"}))
    if att.replay:
        await ws.send_bytes(att.replay)
    outgoing = asyncio.create_task(_forward_output(ws, att))
    incoming = asyncio.create_task(_forward_input(ws, att))
    done, pending = await asyncio.wait({outgoing, incoming}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    for task in done:
        task.result()  # re-raise unexpected errors (logged by the caller, type only)


async def _forward_output(ws: WebSocket, att: TerminalAttachment) -> None:
    closed = False
    while (item := await att.recv()) is not None:
        if isinstance(item, bytes):
            await ws.send_bytes(item)
        else:
            closed = item.state is TerminalState.CLOSED
            await ws.send_text(json.dumps({"type": "state", **terminal_json(item)}))
    with contextlib.suppress(Exception):
        await ws.close(code=1000 if closed else 4001)


async def _forward_input(ws: WebSocket, att: TerminalAttachment) -> None:
    while True:
        try:
            message = await ws.receive()
        except WebSocketDisconnect:
            return
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None:
            if len(data) > MAX_INPUT_FRAME:
                await ws.close(code=1009)
                return
            await att.write(data)
            continue
        text = message.get("text")
        if text is not None:
            with contextlib.suppress(ValueError):
                control = ResizeMessage.model_validate_json(text)
                if control.type == "resize":
                    att.resize(control.cols, control.rows)
