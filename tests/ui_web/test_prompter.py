"""prompter-web: prompts in the browser, first answer wins, timeout, and an OTP login
end-to-end against the in-process SSH server (secret never logged or stored)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml

from simmanager.contracts import auth as auth_contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts.auth import AuthPrompter, AuthRequest, Prompt
from simmanager.contracts.storage import Storage
from simmanager.plugins.storage_sqlite import SqliteStoragePlugin
from simmanager.plugins.transport_ssh import SshTransportPlugin
from simmanager.plugins.ui_web import WebPrompterPlugin

from ..transport.sshserver import SshTestServer
from .webclient import Web, recv_type, running_web, ws_connect

OTP = "otp-7f3a91c2"
REQUEST = AuthRequest(
    cluster_id="c1",
    kind="keyboard-interactive",
    title="c1: login",
    prompts=(Prompt("Verification code: ", echo=False),),
)


@pytest.fixture
async def web() -> AsyncIterator[Web]:
    async with running_web(
        WebPrompterPlugin, config={"prompter-web": {"prompt_timeout_s": 5}}
    ) as w:
        yield w


def _prompter(web: Web) -> AuthPrompter:
    return web.kernel.services.require(auth_contract.SERVICE_KEY)  # type: ignore[no-any-return]


async def _send(ws: object, message: dict[str, object]) -> None:
    await ws.send(json.dumps(message))  # type: ignore[attr-defined]


async def test_first_answer_wins_and_late_browser_sees_pending(web: Web) -> None:
    prompter = _prompter(web)
    async with ws_connect(web) as early:
        await recv_type(early, "hello")
        ask = asyncio.create_task(prompter.ask(REQUEST))
        prompt = await recv_type(early, "auth.prompt")
        assert prompt["prompts"] == [{"text": "Verification code: ", "echo": False}]
        async with ws_connect(web) as late:
            pending = await recv_type(late, "auth.prompt")  # sent on connect
            assert pending["prompt_id"] == prompt["prompt_id"]
            pid = prompt["prompt_id"]
            # Malformed answers are ignored: wrong count, wrong type, unknown id.
            await _send(late, {"type": "auth.answer", "prompt_id": pid, "values": ["a", "b"]})
            await _send(late, {"type": "auth.answer", "prompt_id": pid, "values": [1]})
            await _send(late, {"type": "auth.answer", "prompt_id": "nope", "values": ["x"]})
            await _send(late, {"type": "auth.confirm_answer", "prompt_id": pid, "yes": True})
            await _send(late, {"type": "auth.answer", "prompt_id": pid, "values": [OTP]})
            assert await asyncio.wait_for(ask, 5) == [OTP]
            # A later answer from another browser changes nothing.
            await _send(early, {"type": "auth.answer", "prompt_id": pid, "values": ["other"]})
            assert (await recv_type(early, "auth.closed"))["prompt_id"] == pid
            assert (await recv_type(late, "auth.closed"))["prompt_id"] == pid


async def test_confirm_and_cancel(web: Web) -> None:
    prompter = _prompter(web)
    async with ws_connect(web) as ws:
        await recv_type(ws, "hello")
        confirm = asyncio.create_task(prompter.confirm("c1", "Renew the certificate?"))
        msg = await recv_type(ws, "auth.confirm")
        assert msg["question"] == "Renew the certificate?"
        await _send(ws, {"type": "auth.answer", "prompt_id": msg["prompt_id"], "values": []})
        await _send(ws, {"type": "auth.confirm_answer", "prompt_id": msg["prompt_id"], "yes": True})
        assert await asyncio.wait_for(confirm, 5) is True

        ask = asyncio.create_task(prompter.ask(REQUEST))
        msg = await recv_type(ws, "auth.prompt")
        await _send(ws, {"type": "auth.cancel", "prompt_id": msg["prompt_id"]})
        assert await asyncio.wait_for(ask, 5) is None


async def test_unanswered_prompt_times_out() -> None:
    async with running_web(
        WebPrompterPlugin, config={"prompter-web": {"prompt_timeout_s": 0.2}}
    ) as web:
        prompter = _prompter(web)
        assert await prompter.ask(REQUEST) is None  # no browser open
        assert await prompter.confirm("c1", "?") is False
        assert web.server.client_count() == 0


async def test_unloading_ends_pending_prompts(web: Web) -> None:
    ask = asyncio.create_task(_prompter(web).ask(REQUEST))
    await asyncio.sleep(0.05)
    await web.kernel.stop()
    assert await asyncio.wait_for(ask, 5) is None


@pytest.fixture
async def ssh_server(tmp_path: Path) -> AsyncIterator[SshTestServer]:
    srv = SshTestServer(tmp_path / "ssh")
    srv.otp = OTP
    await srv.start()
    yield srv
    await srv.stop()


async def test_otp_login_end_to_end(
    tmp_path: Path, ssh_server: SshTestServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    profile = ssh_server.profile(auth=["keyboard-interactive"])
    clusters = tmp_path / "clusters.yaml"
    clusters.write_text(
        yaml.safe_dump({"clusters": {"c1": profile.model_dump(mode="json", exclude_unset=True)}}),
        encoding="utf-8",
    )
    db = tmp_path / "sim.db"
    config = {
        "storage-sqlite": {"path": str(db)},
        "transport-ssh": {"clusters_file": str(clusters)},
    }
    async with running_web(
        SqliteStoragePlugin, SshTransportPlugin, WebPrompterPlugin, config=config
    ) as web:
        async with ws_connect(web) as ws:
            await recv_type(ws, "hello")
            r = await web.post_json("/api/clusters/c1/reconnect")  # the UI button
            assert r.status_code == 202
            prompt = await recv_type(ws, "auth.prompt")
            assert prompt["cluster_id"] == "c1" and prompt["kind"] == "keyboard-interactive"
            await _send(
                ws, {"type": "auth.answer", "prompt_id": prompt["prompt_id"], "values": [OTP]}
            )
            for _ in range(10):
                state = await recv_type(ws, "cluster.state")
                if state["state"] == "connected":
                    break
            assert state["state"] == "connected"

        storage: Storage = web.kernel.services.require(storage_contract.SERVICE_KEY)
        events = await storage.events()
        assert "cluster.connected" in [e.kind for e in events]
        assert all(OTP not in json.dumps(e.payload) for e in events)

    # Server side only: the test's own clients (httpx, websockets.client) log their traffic.
    client_side = ("httpx", "httpcore", "websockets.client")
    server_side = [r for r in caplog.records if not r.name.startswith(client_side)]
    leaks = [(r.name, r.getMessage()) for r in server_side if OTP in r.getMessage()]
    assert server_side and not leaks, leaks
    assert OTP.encode() not in db.read_bytes()
