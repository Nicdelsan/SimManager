"""Interactive terminals (`open_pty`) against the in-process SSH server's fake shell."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from simmanager.contracts.transport import (
    PtyChannel,
    RemoteDirectoryMissingError,
    TerminalLimitError,
    TmuxSpec,
)
from simmanager.plugins.storage_sqlite import SqliteStorage
from simmanager.plugins.transport_ssh import SshTransport
from simmanager.plugins.transport_ssh.pty import pty_command

from .sshserver import USER, SshTestServer

C = "c1"


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[SshTestServer]:
    srv = SshTestServer(tmp_path / "ssh")
    srv.commands["hostname"] = ("login01\n", "", 0)
    (srv.sftp_root / "work" / "case 1").mkdir(parents=True)
    await srv.start()
    yield srv
    await srv.stop()


@pytest.fixture
async def transport(tmp_path: Path, server: SshTestServer) -> AsyncIterator[SshTransport]:
    storage = await SqliteStorage.open(str(tmp_path / "t.db"))

    async def emit(event: str, payload: object) -> None:
        pass

    key = server.new_client_key("id_test")
    t = SshTransport(
        {C: server.profile(key_files=[str(key)], max_terminals=2)},
        storage=storage,
        emit=emit,
        prompter=lambda: None,
    )
    yield t
    await t.close()
    await storage.close()


async def read_until(pty: PtyChannel, marker: bytes, wait_s: float = 5) -> bytes:
    buf = b""

    async def loop() -> bytes:
        nonlocal buf
        while marker not in buf:
            chunk = await pty.read()
            if not chunk:
                raise EOFError(buf)
            buf += chunk
        return buf

    return await asyncio.wait_for(loop(), wait_s)


async def test_round_trip_resize_and_exit(transport: SshTransport, server: SshTestServer) -> None:
    session = await transport.session(C)
    pty = await session.open_pty(cols=100, rows=30)
    assert server.pty_commands == [None]  # plain shell request: sshd starts a login shell
    assert b"xterm-256color 100x30" in await read_until(pty, b"$ ")

    await pty.write("echo héllo\r".encode())
    assert "héllo".encode() in await read_until(pty, "héllo\r\n".encode())

    pty.resize(120, 40)
    await read_until(pty, b"size 120x40")

    await pty.write(b"pwd\r")
    await read_until(pty, f"/home/{USER}\r\n".encode())

    await pty.write(b"exit 3\r")
    with pytest.raises(EOFError):
        await read_until(pty, b"never")
    await pty.wait_closed()
    assert pty.exit_status == 3
    assert await pty.read() == b""


async def test_cwd_is_quoted_and_checked(transport: SshTransport, server: SshTestServer) -> None:
    session = await transport.session(C)
    pty = await session.open_pty(cols=80, rows=24, cwd="/work/case 1")
    await read_until(pty, b"$ ")
    await pty.write(b"pwd\r")
    await read_until(pty, b"/work/case 1\r\n")
    assert server.pty_commands[-1] == (
        "cd -- '/work/case 1' 2>/dev/null || "
        "printf 'SimManager: folder not found: %s\\n' '/work/case 1'; "
        'exec "${SHELL:-/bin/sh}" -l'
    )
    pty.close()
    await pty.wait_closed()

    with pytest.raises(RemoteDirectoryMissingError) as info:
        await session.open_pty(cols=80, rows=24, cwd="/work/gone")
    assert info.value.path == "/work/gone"
    assert transport._clusters[C].terminals == 0  # failed opens release their slot


async def test_terminal_limit_never_takes_monitor_channels(
    transport: SshTransport, server: SshTestServer
) -> None:
    session = await transport.session(C)
    ptys = [await session.open_pty(cols=80, rows=24) for _ in range(2)]
    with pytest.raises(TerminalLimitError, match="2 terminal"):
        await session.open_pty(cols=80, rows=24)

    # The monitor's commands and file reads keep working with every terminal open.
    (server.sftp_root / "f.txt").write_bytes(b"abc")
    results = await asyncio.gather(*(session.run_readonly(["hostname"]) for _ in range(8)))
    assert all(r.ok for r in results)
    assert await session.read_range("/f.txt", 0, 10) == b"abc"

    ptys[0].close()
    await ptys[0].wait_closed()
    for _ in range(50):  # the slot is released by a background task
        if transport._clusters[C].terminals < 2:
            break
        await asyncio.sleep(0.02)
    third = await session.open_pty(cols=80, rows=24)
    await read_until(third, b"$ ")
    for p in (ptys[1], third):
        p.close()


async def test_connection_loss_ends_the_terminal(
    transport: SshTransport, server: SshTestServer
) -> None:
    session = await transport.session(C)
    pty = await session.open_pty(cols=80, rows=24)
    await read_until(pty, b"$ ")
    await transport.reset(C)
    with pytest.raises(EOFError):
        await read_until(pty, b"never")
    assert pty.exit_status is None


def test_pty_commands() -> None:
    assert pty_command(None, None) is None
    new = pty_command("/w/a'b", TmuxSpec("simman-c1-3"))
    assert new is not None
    assert "exec tmux new-session -s simman-c1-3 -c '/w/a'\"'\"'b'" in new
    assert "-A" not in new.split()  # never joins an existing session by accident
    assert new.index("command -v tmux") < new.index("exec tmux")
    attach = pty_command(None, TmuxSpec("simman-c1-1", attach=True))
    assert attach is not None and attach.endswith("exec tmux attach-session -t =simman-c1-1")
