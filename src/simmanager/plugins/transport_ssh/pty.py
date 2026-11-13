"""Interactive terminals (M4): the remote command line of a PTY and the channel wrapper.

The terminal is the *user* acting on the cluster (ADR 0007). SimManager only chooses how
the login shell starts (folder, tmux); it never writes into the channel itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex

import asyncssh

from simmanager.contracts.transport import TmuxSpec

LOGIN_SHELL = 'exec "${SHELL:-/bin/sh}" -l'


def pty_command(cwd: str | None, tmux: TmuxSpec | None) -> str | None:
    """Remote command for `open_pty`; None means a plain `shell` request (login shell).

    ASSUMPTION(A400): the login shell on the clusters is POSIX (bash/zsh): `cd --`, `||`,
    `${VAR:-x}` and `command -v` are used here.
    """
    parts: list[str] = []
    if cwd is not None:
        q = shlex.quote(cwd)
        # The folder is checked over SFTP before opening; this covers the race.
        parts.append(f"cd -- {q} 2>/dev/null || printf 'SimManager: folder not found: %s\\n' {q}")
    if tmux is not None:
        name = shlex.quote(tmux.session)
        parts.append(
            "command -v tmux >/dev/null 2>&1 || "
            "{ echo 'SimManager: tmux not found on this cluster, opening a plain shell'; "
            f"{LOGIN_SHELL}; }}"
        )
        if tmux.attach:
            parts.append(f"exec tmux attach-session -t {shlex.quote('=' + tmux.session)}")
        else:
            # No -A: an existing name makes tmux fail instead of joining an old session.
            where = f" -c {shlex.quote(cwd)}" if cwd is not None else ""
            parts.append(f"exec tmux new-session -s {name}{where}")
    elif cwd is not None:
        parts.append(LOGIN_SHELL)
    return "; ".join(parts) if parts else None


class SshPty:
    """Implements `simmanager.contracts.transport.PtyChannel` on an asyncssh process."""

    def __init__(self, process: asyncssh.SSHClientProcess[bytes]) -> None:
        self._p = process

    async def read(self, max_bytes: int = 65536) -> bytes:
        try:
            data = await self._p.stdout.read(max_bytes)
        except (asyncssh.Error, OSError, asyncio.IncompleteReadError):
            return b""
        return bytes(data)

    async def write(self, data: bytes) -> None:
        if self._p.stdin.is_closing():
            return
        try:
            self._p.stdin.write(data)
            await self._p.stdin.drain()
        except (asyncssh.Error, OSError, BrokenPipeError):
            pass  # the shell ended; read() reports it

    def resize(self, cols: int, rows: int) -> None:
        with contextlib.suppress(asyncssh.Error, OSError):
            self._p.change_terminal_size(cols, rows)

    def close(self) -> None:
        self._p.close()

    async def wait_closed(self) -> None:
        await self._p.wait_closed()

    @property
    def exit_status(self) -> int | None:
        status = self._p.exit_status
        return None if status is None or status < 0 else status
