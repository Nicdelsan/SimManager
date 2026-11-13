"""Terminal sessions against the in-process SSH server's fake shell."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from simmanager.contracts.terminal import (
    TerminalError,
    TerminalInfo,
    TerminalRequest,
    TerminalState,
)
from simmanager.contracts.transport import ClusterUnavailableError, ConnectionState
from simmanager.plugins.storage_sqlite import SqliteStorage
from simmanager.plugins.terminal import RingBuffer, TerminalManager
from simmanager.plugins.transport_ssh import SshTransport

from ..transport.sshserver import FakePrompter, SshTestServer

C = "c1"
TMUX_LS = "tmux list-sessions -F '#{session_name}'"


class Env:
    def __init__(self, server: SshTestServer, storage: SqliteStorage) -> None:
        self.server = server
        self.storage = storage
        self.bus: list[tuple[str, Any]] = []
        self.prompter = FakePrompter()
        self.now = 1000.0
        self.transport: SshTransport | None = None
        self.manager: TerminalManager | None = None

    def start(self, **profile: Any) -> TerminalManager:
        async def emit(event: str, payload: Any) -> None:
            self.bus.append((event, payload))

        if "key_files" not in profile and "auth" not in profile:
            profile["key_files"] = [str(self.server.new_client_key("id_test"))]
        self.transport = SshTransport(
            {C: self.server.profile(**profile)},
            storage=self.storage,
            emit=emit,
            prompter=lambda: self.prompter,
        )
        transport = self.transport
        self.manager = TerminalManager(
            lambda: transport,
            self.storage,
            emit,
            logging.getLogger("test.terminal"),
            orphan_timeout_s=60,
            buffer_bytes=4096,
            clock=lambda: self.now,
        )
        return self.manager

    async def events(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (e.kind, dict(e.payload))
            for e in await self.storage.events()
            if e.kind.startswith("terminal.")
        ]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    server = SshTestServer(tmp_path / "ssh")
    (server.sftp_root / "work" / "case1").mkdir(parents=True)
    await server.start()
    storage = await SqliteStorage.open(str(tmp_path / "t.db"))
    e = Env(server, storage)
    yield e
    if e.manager:
        await e.manager.close_all()
    if e.transport:
        await e.transport.close()
    await storage.close()
    await server.stop()


async def collect(
    att: Any, until: Callable[[bytes, TerminalInfo | None], bool], wait_s: float = 5
) -> tuple[bytes, TerminalInfo | None]:
    """Read an attachment until `until(output, last_state)` holds."""
    out, last = b"", None

    async def loop() -> tuple[bytes, TerminalInfo | None]:
        nonlocal out, last
        while not until(out, last):
            item = await att.recv()
            if item is None:
                raise EOFError(out, last)
            if isinstance(item, bytes):
                out += item
            else:
                last = item
        return out, last

    return await asyncio.wait_for(loop(), wait_s)


def has(marker: bytes) -> Callable[[bytes, TerminalInfo | None], bool]:
    return lambda out, _: marker in out


def state_is(state: TerminalState) -> Callable[[bytes, TerminalInfo | None], bool]:
    return lambda _, last: last is not None and last.state is state


async def test_open_type_and_close(env: Env) -> None:
    manager = env.start()
    info = await manager.open(TerminalRequest(C, cwd="/work/case1", run_id=None, cols=90))
    assert info.state is TerminalState.CONNECTING and info.title == "c1:case1"
    att = manager.attach(info.id)
    await collect(att, has(b"xterm-256color 90x24"))
    await att.write(b"pwd\r")
    await collect(att, has(b"/work/case1\r\n"))
    att.resize(100, 30)
    await collect(att, has(b"size 100x30"))

    await manager.close(info.id)
    _, _last = await collect(att, state_is(TerminalState.CLOSED))
    assert await att.recv() is None
    assert manager.sessions() == []
    events = await env.events()
    assert [k for k, _ in events] == ["terminal.opened", "terminal.closed"]
    assert events[0][1] == {"session_id": info.id, "cwd": "/work/case1", "tmux_session": None}
    assert events[1][1]["reason"] == "user" and "duration_s" in events[1][1]
    states = [p.state for e, p in env.bus if e == "terminal.state"]
    assert states[0] is TerminalState.CONNECTING and TerminalState.OPEN in states


async def test_reattach_replays_buffer_and_takes_over(env: Env) -> None:
    manager = env.start()
    info = await manager.open(TerminalRequest(C))
    first = manager.attach(info.id)
    await collect(first, has(b"$ "))
    await first.write(b"echo before-reload\r")
    first.detach()
    for _ in range(100):
        if b"before-reload\r\n$ " in manager._sessions[info.id].buffer.snapshot():
            break
        await asyncio.sleep(0.02)
    assert manager.get(info.id) is not None and not manager.get(info.id).attached  # type: ignore[union-attr]

    second = manager.attach(info.id)
    assert b"before-reload" in second.replay and b"xterm-256color" in second.replay
    third = manager.attach(info.id)  # another tab takes over
    while (item := await asyncio.wait_for(second.recv(), 2)) is not None:
        assert not isinstance(item, bytes) or item == b""
    await third.write(b"echo after\r")
    await collect(third, has(b"after\r\n"))


async def test_orphan_expires(env: Env) -> None:
    manager = env.start()
    info = await manager.open(TerminalRequest(C))
    att = manager.attach(info.id)
    await collect(att, state_is(TerminalState.OPEN))
    att.detach()
    env.now += 59
    await manager.sweep()
    assert manager.get(info.id) is not None
    env.now += 2
    await manager.sweep()
    assert manager.get(info.id) is None
    events = await env.events()
    assert events[-1][0] == "terminal.closed" and events[-1][1]["reason"] == "expired"


async def test_shell_exit_is_reported(env: Env) -> None:
    manager = env.start()
    info = await manager.open(TerminalRequest(C))
    att = manager.attach(info.id)
    await collect(att, has(b"$ "))
    await att.write(b"exit 4\r")
    _, last = await collect(att, state_is(TerminalState.EXITED))
    assert last is not None and last.exit_status == 4
    events = await env.events()
    assert events[-1][1]["reason"] == "exited" and events[-1][1]["exit_status"] == 4
    await att.write(b"ignored\r")  # no shell any more: dropped silently
    await manager.close(info.id)
    assert [k for k, _ in await env.events()].count("terminal.closed") == 1


async def test_failures_are_explained(env: Env) -> None:
    manager = env.start(max_terminals=1)
    missing = await manager.open(TerminalRequest(C, cwd="/work/gone"))
    att = manager.attach(missing.id)
    _, last = await collect(att, state_is(TerminalState.FAILED))
    assert last is not None and last.error is TerminalError.MISSING_DIR
    assert "/work/gone" in last.detail

    ok = await manager.open(TerminalRequest(C))
    await collect(manager.attach(ok.id), state_is(TerminalState.OPEN))
    over = await manager.open(TerminalRequest(C))
    _, last = await collect(manager.attach(over.id), state_is(TerminalState.FAILED))
    assert last is not None and last.error is TerminalError.LIMIT
    with pytest.raises(KeyError):
        await manager.open(TerminalRequest("nope"))
    assert [k for k, _ in await env.events()] == ["terminal.opened"]  # failures: no events


async def test_awaiting_auth_prompts_once_then_opens(env: Env) -> None:
    env.server.otp = "123456"
    manager = env.start(auth=["keyboard-interactive"])
    assert env.transport is not None
    env.prompter.answers = [None]  # the monitor's attempt: nobody answered
    with pytest.raises(ClusterUnavailableError):
        await env.transport.session(C)
    assert env.transport.state(C) is ConnectionState.AWAITING_AUTH

    env.prompter.answers = [["123456"]]
    info = await manager.open(TerminalRequest(C))
    att = manager.attach(info.id)
    _out, _last = await collect(att, has(b"$ "))
    assert len(env.prompter.asked) == 2 and env.prompter.asked[1].kind == "keyboard-interactive"
    states = [p.state for e, p in env.bus if e == "terminal.state" and p.id == info.id]
    assert TerminalState.AWAITING_AUTH in states and states[-1] is TerminalState.OPEN


async def test_rejected_auth_is_not_retried(env: Env) -> None:
    env.server.otp = "123456"
    manager = env.start(auth=["keyboard-interactive"])
    env.prompter.answers = [["000000"]]
    info = await manager.open(TerminalRequest(C))
    _, last = await collect(manager.attach(info.id), state_is(TerminalState.FAILED))
    assert last is not None and last.error is TerminalError.UNAVAILABLE
    # (the server re-asks within the same login, as sshd does; nobody answered)
    attempts, asked = env.server.auth_attempts, len(env.prompter.asked)
    await asyncio.sleep(0.3)
    assert env.server.auth_attempts == attempts and len(env.prompter.asked) == asked


async def test_tmux_names_never_reuse_existing_sessions(env: Env) -> None:
    env.server.commands[TMUX_LS] = ("simman-c1-4\nsimman-c1-x\nother\nsimman-c2-9\n", "", 0)
    manager = env.start(terminal_tmux=True)
    info = await manager.open(TerminalRequest(C, cwd="/work/case1"))
    _, last = await collect(manager.attach(info.id), state_is(TerminalState.OPEN))
    assert last is not None and last.tmux_session == "simman-c1-5"
    assert "exec tmux new-session -s simman-c1-5 -c /work/case1" in str(env.server.pty_commands[-1])
    second = await manager.open(TerminalRequest(C))  # same listing: still a new name
    _, last = await collect(manager.attach(second.id), state_is(TerminalState.OPEN))
    assert last is not None and last.tmux_session == "simman-c1-6"
    plain = await manager.open(TerminalRequest(C, tmux=False))
    _, last = await collect(manager.attach(plain.id), state_is(TerminalState.OPEN))
    assert last is not None and last.tmux_session is None
    assert env.server.pty_commands[-1] is None

    assert await manager.tmux_sessions(C) == ["simman-c1-4", "simman-c1-x"]
    await manager.close(info.id)
    attach = await manager.open(TerminalRequest(C, attach_tmux="simman-c1-4"))
    _, last = await collect(manager.attach(attach.id), state_is(TerminalState.OPEN))
    assert str(env.server.pty_commands[-1]).endswith("exec tmux attach-session -t =simman-c1-4")


def test_ring_buffer_keeps_the_tail() -> None:
    buf = RingBuffer(10)
    for part in (b"abcd", b"efgh", b"ijkl"):
        buf.append(part)
    assert buf.snapshot() == b"cdefghijkl"
    buf.append(b"0123456789ABC")
    assert buf.snapshot() == b"3456789ABC"
