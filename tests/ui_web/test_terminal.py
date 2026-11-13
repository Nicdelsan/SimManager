"""Terminal endpoints and the per-session WebSocket, against the fake SSH shell."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.plugins.storage_sqlite import SqliteStoragePlugin, SqliteTimeSeriesPlugin
from simmanager.plugins.terminal import TerminalPlugin
from simmanager.plugins.transport_ssh import SshTransportPlugin
from simmanager.plugins.ui_web import WebPrompterPlugin
from simmanager.plugins.ui_web.quick import input_candidates, quick_actions
from simmanager.plugins.ui_web.security import CSP, TERMINAL_CSP
from simmanager.plugins.ui_web.terminal import Tickets

from ..transport.sshserver import OUTPUT_ONLY, SshTestServer
from .webclient import Web, recv_type, running_web, ws_connect, ws_handshake_status

C = "c1"
TMUX_LS = "tmux list-sessions -F '#{session_name}'"


@pytest.fixture
async def ssh(tmp_path: Path) -> AsyncIterator[SshTestServer]:
    server = SshTestServer(tmp_path / "ssh")
    (server.sftp_root / "work" / "case1").mkdir(parents=True)
    await server.start()
    yield server
    await server.stop()


def _clusters_file(tmp_path: Path, ssh: SshTestServer, **profile: Any) -> Path:
    if "auth" not in profile:
        profile["key_files"] = [str(ssh.new_client_key("id_test"))]
    data = ssh.profile(**profile).model_dump(mode="json", exclude_defaults=True)
    path = tmp_path / "clusters.yaml"
    path.write_text(yaml.safe_dump({"clusters": {C: data}}), encoding="utf-8")
    return path


def _config(tmp_path: Path, clusters: Path, **extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "storage-sqlite": {"path": str(tmp_path / "sim.db")},
        "transport-ssh": {"clusters_file": str(clusters)},
        **extra,
    }


@pytest.fixture
async def web(tmp_path: Path, ssh: SshTestServer) -> AsyncIterator[Web]:
    config = _config(tmp_path, _clusters_file(tmp_path, ssh))
    plugins = (SqliteStoragePlugin, SqliteTimeSeriesPlugin, SshTransportPlugin, TerminalPlugin)
    async with running_web(*plugins, config=config) as w:
        yield w


async def _open(web: Web, **body: Any) -> dict[str, Any]:
    r = await web.post_json("/api/terminals", {"cluster_id": C, **body})
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _term(web: Web, session_id: str, ticket: str, **kw: Any) -> Any:
    return connect(
        f"ws://127.0.0.1:{web.port}/ws/term/{session_id}?ticket={ticket}",
        origin=kw.pop("origin", web.origin),
        additional_headers={"Cookie": web.cookie},
        open_timeout=5,
        **kw,
    )


async def _read_until(ws: Any, marker: bytes, wait_s: float = 5) -> tuple[bytes, list[Any]]:
    """Binary output until `marker`; also returns the JSON control messages seen."""
    out, controls = b"", []

    async def loop() -> None:
        nonlocal out
        while marker not in out:
            message = await ws.recv()
            if isinstance(message, bytes):
                out += message
            else:
                controls.append(json.loads(message))

    await asyncio.wait_for(loop(), wait_s)
    return out, controls


async def test_terminal_round_trip(web: Web) -> None:
    opened = await _open(web, cwd="/work/case1/", cols=100, rows=30)
    session = opened["session"]
    assert session["state"] == "connecting" and session["cwd"] == "/work/case1"
    async with ws_connect(web) as dashboard, _term(web, session["id"], opened["ticket"]) as ws:
        out, controls = await _read_until(ws, b"$ ")
        assert controls[0] == {"type": "replay"}
        assert b"xterm-256color 100x30" in out
        await ws.send(b"pwd\r")
        await _read_until(ws, b"/work/case1\r\n")
        await ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        await _read_until(ws, b"size 120x40")
        await ws.send(json.dumps({"type": "resize", "cols": 1}))  # invalid: ignored
        await ws.send("not json")
        await ws.send(b"echo still here\r")
        await _read_until(ws, b"still here\r\n")
        state = await recv_type(dashboard, "terminal.state")
        assert state["id"] == session["id"]

        async with web.client() as c:
            listing = (await c.get("/api/terminals")).json()
            clusters = (await c.get("/api/clusters")).json()
        assert [(s["id"], s["state"], s["attached"]) for s in listing] == [
            (session["id"], "open", True)
        ]
        assert clusters[0]["terminals"] == {"available": True, "max": 3, "tmux": False, "open": 1}


async def test_ticket_is_single_use_and_bound(web: Web) -> None:
    a, b = await _open(web), await _open(web)
    a_id, b_id = a["session"]["id"], b["session"]["id"]
    path = f"/ws/term/{a_id}?ticket={a['ticket']}"
    good = [("Origin", web.origin), ("Cookie", web.cookie)]
    # Rejected by the middleware: the ticket is not even looked at (still valid after).
    assert await ws_handshake_status(web.port, path=path, headers=good[:1]) == 403
    foreign = [("Origin", "http://evil.example"), ("Cookie", web.cookie)]
    assert await ws_handshake_status(web.port, path=path, headers=foreign) == 403
    assert await ws_handshake_status(web.port, path=path, headers=good, host="evil:1") == 403
    # Wrong session, missing or unknown ticket.
    for bad in (
        f"/ws/term/{b_id}?ticket={a['ticket']}",  # consumes a's ticket: bound to a
        f"/ws/term/{a_id}?ticket={a['ticket']}",  # already used
        f"/ws/term/{a_id}",
        f"/ws/term/{a_id}?ticket=forged",
    ):
        assert await ws_handshake_status(web.port, path=bad, headers=good) == 403, bad
    async with _term(web, b_id, b["ticket"]) as ws:
        await _read_until(ws, b"$ ")
    with pytest.raises(InvalidStatus):
        async with _term(web, b_id, b["ticket"]):
            pass

    r = await web.post_json(f"/api/terminals/{a_id}/ticket")
    async with _term(web, a_id, r.json()["ticket"]) as ws:
        await _read_until(ws, b"$ ")


def test_tickets_expire() -> None:
    now = [0.0]
    tickets = Tickets(ttl_s=30, clock=lambda: now[0])
    t1, t2 = tickets.issue("s1"), tickets.issue("s1")
    now[0] = 29.9
    assert tickets.consume("s1", t1)
    now[0] = 30.1
    assert not tickets.consume("s1", t2)
    assert not tickets.consume("s1", None)


async def test_reattach_replays_and_takes_over(web: Web) -> None:
    opened = await _open(web)
    sid = opened["session"]["id"]
    async with _term(web, sid, opened["ticket"]) as ws:
        await _read_until(ws, b"$ ")
        await ws.send(b"echo before-reload\r")
        await _read_until(ws, b"before-reload\r\n")
    await asyncio.sleep(0.1)
    async with web.client() as c:
        assert (await c.get("/api/terminals")).json()[0]["attached"] is False

    ticket = (await web.post_json(f"/api/terminals/{sid}/ticket")).json()["ticket"]
    async with _term(web, sid, ticket) as first:
        out, controls = await _read_until(first, b"before-reload")
        assert controls[0] == {"type": "replay"} and b"xterm-256color" in out
        ticket = (await web.post_json(f"/api/terminals/{sid}/ticket")).json()["ticket"]
        async with _term(web, sid, ticket) as second:
            await _read_until(second, b"before-reload")
            with pytest.raises(ConnectionClosed) as info:
                while True:
                    await asyncio.wait_for(first.recv(), 5)
            assert info.value.rcvd is not None and info.value.rcvd.code == 4001
            await second.send(b"echo after\r")
            await _read_until(second, b"after\r\n")


async def test_close_and_limits(web: Web) -> None:
    opened = await _open(web)
    sid = opened["session"]["id"]
    async with _term(web, sid, opened["ticket"]) as ws:
        await _read_until(ws, b"$ ")
        r = await web.post_json(f"/api/terminals/{sid}/close")
        assert r.status_code == 200
        with pytest.raises(ConnectionClosed) as info:
            while True:
                await asyncio.wait_for(ws.recv(), 5)
        assert info.value.rcvd is not None and info.value.rcvd.code == 1000
    assert (await web.post_json(f"/api/terminals/{sid}/close")).status_code == 404
    assert (await web.post_json(f"/api/terminals/{sid}/ticket")).status_code == 404
    assert (await web.post_json("/api/terminals", {"cluster_id": "nope"})).status_code == 404
    bad = await web.post_json("/api/terminals", {"cluster_id": C, "cwd": "relative/dir"})
    assert bad.status_code == 422

    opened = await _open(web)
    async with _term(web, opened["session"]["id"], opened["ticket"], max_size=None) as ws:
        await _read_until(ws, b"$ ")
        await ws.send(b"x" * (64 * 1024 + 1))
        with pytest.raises(ConnectionClosed) as info:
            while True:
                await asyncio.wait_for(ws.recv(), 5)
        assert info.value.rcvd is not None and info.value.rcvd.code == 1009

    storage = web.kernel.services.require("storage")
    kinds = [e.kind for e in await storage.events() if e.kind.startswith("terminal.")]
    assert kinds == ["terminal.opened", "terminal.closed", "terminal.opened"]


async def test_terminal_page_alone_allows_inline_styles(web: Web) -> None:
    async with web.client() as c:
        page = await c.get("/terminal")
        index = await c.get("/")
        api = await c.get("/api/terminals")
    assert page.status_code == 200 and page.headers["content-security-policy"] == TERMINAL_CSP
    assert CSP == "default-src 'self'; frame-ancestors 'none'"
    assert "script-src" not in TERMINAL_CSP and "default-src 'self'" in TERMINAL_CSP
    for other in (index, api):
        assert other.headers["content-security-policy"] == CSP


async def test_terminal_content_never_logged(web: Web, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("asyncssh", "websockets", "uvicorn", "simmanager"):
        caplog.set_level(logging.DEBUG, logger=name)
    typed = "typed-7f3a"
    opened = await _open(web, cwd="/work/case1")
    async with _term(web, opened["session"]["id"], opened["ticket"]) as ws:
        await _read_until(ws, b"$ ")
        await ws.send(f"echo {typed}\r".encode())
        await ws.send(b"hush\r")
        await _read_until(ws, OUTPUT_ONLY.encode() + b"\r\n")
    await web.post_json(f"/api/terminals/{opened['session']['id']}/close")
    storage = web.kernel.services.require("storage")
    stored = json.dumps([[e.kind, e.payload] for e in await storage.events()])
    assert "terminal.closed" in stored
    # The test's own client (websockets.client, httpx) sees the frames, of course.
    client_side = ("websockets.client", "httpx", "httpcore")
    server_records = [r for r in caplog.records if not r.name.startswith(client_side)]
    assert any(r.name.startswith("asyncssh") for r in server_records)  # DEBUG really on
    logged = "\n".join(f"{r.name} {r.getMessage()} {r.args!r}" for r in server_records)
    for marker in (typed, OUTPUT_ONLY):
        assert marker not in logged
        assert marker not in stored


async def test_tmux_sessions_endpoint(web: Web, ssh: SshTestServer) -> None:
    ssh.commands[TMUX_LS] = ("simman-c1-2\nmine\n", "", 0)
    async with web.client() as c:
        assert (await c.get(f"/api/clusters/{C}/tmux")).json() == ["simman-c1-2"]
        assert (await c.get("/api/clusters/nope/tmux")).status_code == 404
    bad = await web.post_json("/api/terminals", {"cluster_id": C, "attach_tmux": "a;b"})
    assert bad.status_code == 422


async def test_awaiting_auth_prompt_in_browser_then_shell(
    tmp_path: Path, ssh: SshTestServer
) -> None:
    ssh.otp = "123456"
    clusters = _clusters_file(tmp_path, ssh, auth=["keyboard-interactive"])
    config = _config(tmp_path, clusters, **{"prompter-web": {"prompt_timeout_s": 1.0}})
    async with running_web(
        SqliteStoragePlugin, SshTransportPlugin, TerminalPlugin, WebPrompterPlugin, config=config
    ) as web:
        transport = web.kernel.services.require("transport")
        # The monitor tried while nobody answered: the cluster now awaits authentication.
        with pytest.raises(Exception, match="awaiting_auth"):
            await transport.session(C)
        assert str(transport.state(C)) == "awaiting_auth"

        async with ws_connect(web) as dashboard:
            opened = await _open(web)
            prompt = await recv_type(dashboard, "auth.prompt")
            await dashboard.send(
                json.dumps(
                    {"type": "auth.answer", "prompt_id": prompt["prompt_id"], "values": ["123456"]}
                )
            )
            async with _term(web, opened["session"]["id"], opened["ticket"]) as ws:
                _, controls = await _read_until(ws, b"$ ")
        states = [m["state"] for m in controls if m["type"] == "state"]
        assert states[-1] == "open"
        assert str(transport.state(C)) == "connected"


async def test_terminals_without_service_answer_503() -> None:
    async with running_web() as web, web.client() as c:
        assert (await c.get("/api/terminals")).status_code == 503


async def test_quick_actions_for_a_run(web: Web, ssh: SshTestServer) -> None:
    case = ssh.sftp_root / "work" / "case 2"
    case.mkdir(parents=True)
    for name in ("engine.in", "inputs.in", "notes.txt"):
        (case / name).write_text("x")
    storage = web.kernel.services.require("storage")
    job = await storage.upsert_job(
        C,
        JobInfo(
            job_id="77",
            name="c2",
            state=JobState.RUNNING,
            raw_state="RUNNING",
            work_dir="/work/case 2",
        ),
        active=True,
    )
    run_id = job.record.run_id
    async with web.client() as c:
        first = (await c.get(f"/api/runs/{run_id}/quick-actions")).json()
        # Not connected yet: no SFTP listing (a page load never triggers a login).
        assert first["note"] and not [a for a in first["actions"] if a["group"] == "Edit input"]
        assert first["job"] == {"job_id": "77", "name": "c2", "active": True, "state": "running"}
        await web.kernel.services.require("transport").session(C)
        data = (await c.get(f"/api/runs/{run_id}/quick-actions")).json()
        assert (await c.get("/api/runs/999/quick-actions")).status_code == 404
    commands = {a["label"]: a["command"] for a in data["actions"]}
    assert data["note"] is None
    assert commands["cd to the run folder"] == "cd '/work/case 2'"
    assert commands["squeue -j 77"] == "squeue -j 77"
    assert commands["vi engine.in"] == "vi '/work/case 2/engine.in'"
    assert [a["label"] for a in data["actions"] if a["group"] == "Edit input"] == [
        "vi engine.in",
        "vi inputs.in",
    ]


def test_quick_action_commands_are_quoted_single_lines() -> None:
    actions = quick_actions(
        work_dir="/w/a b",
        job_id="12_3",
        logs=["/w/a b/log; rm -rf ~"],
        inputs=input_candidates("converge", "/w/a b", ["x.in", "$(id).in", "y.dat"]),
    )
    for a in actions:
        assert not set(a.command) & {"\n", "\r"}
    by_label = {a.label: a.command for a in actions}
    assert by_label["tail -n 100 log; rm -rf ~"] == "tail -n 100 '/w/a b/log; rm -rf ~'"
    assert by_label["vi $(id).in"] == "vi '/w/a b/$(id).in'"
    foam = input_candidates("openfoam", "/c", ["system/controlDict", "system/other"])
    assert foam == ["/c/system/controlDict"]
