"""In-process SSH server (asyncssh) for transport tests. Everything is synthetic."""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncssh

from simmanager.contracts.auth import AuthRequest
from simmanager.contracts.clusters import ClusterProfile

USER = "tester"


@dataclass
class FakePrompter:
    """Answers prompts from a queue and records what was asked."""

    answers: list[list[str] | None] = field(default_factory=list)
    confirm_answer: bool = False
    asked: list[AuthRequest] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    async def ask(self, request: AuthRequest) -> list[str] | None:
        self.asked.append(request)
        return self.answers.pop(0) if self.answers else None

    async def confirm(self, cluster_id: str, question: str) -> bool:
        self.questions.append(question)
        return self.confirm_answer


class SshTestServer:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.authorized: list[asyncssh.SSHKey] = []
        self.ca_keys: list[asyncssh.SSHKey] = []
        self.password: str | None = None
        self.otp: str | None = None
        self.commands: dict[str, tuple[str, str, int]] = {}
        self.executed: list[str] = []
        self.read_stdin: set[str] = set()
        """Commands whose stdin is read to EOF and recorded in `stdin`."""
        self.stdin: dict[str, str] = {}
        self.pty_commands: list[str | None] = []
        """Command of every PTY request (None: plain `shell` request)."""
        self.shells: list[asyncssh.SSHServerProcess[str]] = []
        self.auth_attempts = 0
        self.connections = 0
        self.live: set[asyncssh.SSHServerConnection] = set()
        self.sftp_root = root / "remote"
        self.sftp_root.mkdir(exist_ok=True)
        self.port = 0
        self._server: asyncssh.SSHAcceptor | None = None
        self.known_hosts = root / "known_hosts"
        self.ssh_config = root / "ssh_config"
        self.ssh_config.write_text("", encoding="utf-8")

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._server = await asyncssh.create_server(
            lambda: _Server(self),
            "127.0.0.1",
            self.port,
            server_host_keys=[self.host_key],
            process_factory=self._handle,
            sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(self.sftp_root)),
            reuse_address=True,
            line_editor=False,  # the fake shell sees raw input and window size changes
        )
        self.port = self._server.sockets[0].getsockname()[1]
        public = self.host_key.export_public_key().decode().strip()
        self.known_hosts.write_text(f"[127.0.0.1]:{self.port} {public}\n", encoding="utf-8")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for conn in list(self.live):  # a restart drops established sessions too
                conn.close()
            await self._server.wait_closed()
            self._server = None

    # ------------------------------------------------------------ credentials
    def new_client_key(self, name: str, *, passphrase: str | None = None) -> Path:
        key = asyncssh.generate_private_key("ssh-ed25519")
        path = self.root / name
        if passphrase is None:
            key.write_private_key(str(path))
        else:  # PKCS#8: encrypted OpenSSH-format keys would need the optional bcrypt package
            key.write_private_key(str(path), "pkcs8-pem", passphrase=passphrase)
        key.write_public_key(str(path) + ".pub")
        self.authorized.append(key)
        return path

    def new_cert_key(self, name: str, *, valid_for: timedelta) -> tuple[Path, asyncssh.SSHKey]:
        """A key accepted only through a certificate signed by the test CA."""
        ca = asyncssh.generate_private_key("ssh-ed25519")
        self.ca_keys.append(ca)
        key = asyncssh.generate_private_key("ssh-ed25519")
        path = self.root / name
        key.write_private_key(str(path))
        self.write_cert(ca, key, path, valid_for)
        return path, ca

    @staticmethod
    def write_cert(
        ca: asyncssh.SSHKey, key: asyncssh.SSHKey | Path, path: Path, valid_for: timedelta
    ) -> None:
        if isinstance(key, Path):
            key = asyncssh.read_private_key(str(key))
        now = datetime.now(UTC)
        before = now + valid_for
        after = min(now, before) - timedelta(hours=1)
        cert = ca.generate_user_certificate(
            key, "test", principals=[USER], valid_after=after, valid_before=before
        )
        cert.write_certificate(str(path) + "-cert.pub")

    def profile(self, **overrides: Any) -> ClusterProfile:
        data: dict[str, Any] = {
            "host": "127.0.0.1",
            "port": self.port,
            "user": USER,
            "known_hosts": str(self.known_hosts),
            "ssh_config_file": str(self.ssh_config),
            "connect_timeout_s": 5,
        }
        data.update(overrides)
        return ClusterProfile.model_validate(data)

    # ---------------------------------------------------------------- serving
    async def _handle(self, process: asyncssh.SSHServerProcess[str]) -> None:
        if process.get_terminal_type() is not None:
            await _fake_shell(self, process)
            return
        command = str(process.command or "")
        self.executed.append(command)
        if command in self.read_stdin:
            self.stdin[command] = await process.stdin.read()
        stdout, stderr, status = self.commands.get(command, ("", f"unknown: {command}\n", 127))
        process.stdout.write(stdout)
        process.stderr.write(stderr)
        process.exit(status)


class _Server(asyncssh.SSHServer):
    def __init__(self, owner: SshTestServer) -> None:
        self._o = owner

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._o.connections += 1
        self._o.live.add(conn)
        self._conn = conn

    def connection_lost(self, exc: Exception | None) -> None:
        self._o.live.discard(self._conn)

    def begin_auth(self, username: str) -> bool:
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        self._o.auth_attempts += 1
        mine = key.export_public_key()
        return username == USER and any(k.export_public_key() == mine for k in self._o.authorized)

    def validate_ca_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        self._o.auth_attempts += 1
        mine = key.export_public_key()
        return any(k.export_public_key() == mine for k in self._o.ca_keys)

    def password_auth_supported(self) -> bool:
        return self._o.password is not None

    def validate_password(self, username: str, password: str) -> bool:
        self._o.auth_attempts += 1
        return username == USER and password == self._o.password

    def kbdint_auth_supported(self) -> bool:
        return self._o.otp is not None

    def get_kbdint_challenge(
        self, username: str, lang: str, submethods: str
    ) -> tuple[str, str, str, Sequence[tuple[str, bool]]]:
        return "OTP", "Enter the code from your app", "", [("Code: ", False)]

    def validate_kbdint_response(self, username: str, responses: Sequence[str]) -> bool:
        self._o.auth_attempts += 1
        return list(responses) == [self._o.otp]


CRLF = "\r\n"


OUTPUT_ONLY = "output-only-91c2"
"""Printed by the fake shell's `hush` command, never typed (privacy tests)."""


async def _fake_shell_loop(owner: SshTestServer, process: asyncssh.SSHServerProcess[str]) -> None:
    """A tiny interactive shell: `pwd`, `echo ...`, `exit N`, `sleep N`; prints resizes."""
    command = process.command
    cwd = f"/home/{USER}"
    if command and command.startswith("cd -- "):
        cwd = shlex.split(command.split(" 2>/dev/null", 1)[0])[2]
    cols, rows, *_ = process.get_terminal_size()
    out = process.stdout
    out.write(f"{process.get_terminal_type()} {cols}x{rows}{CRLF}$ ")
    while True:
        try:
            line = await process.stdin.readuntil(["\r", "\n"])
        except asyncio.IncompleteReadError:
            break
        except asyncssh.TerminalSizeChanged as exc:
            out.write(f"size {exc.width}x{exc.height}{CRLF}")
            continue
        except asyncssh.BreakReceived:
            continue
        out.write(line.rstrip("\r\n") + CRLF)  # echo, as a terminal line discipline does
        word, _, rest = line.strip().partition(" ")
        if word == "pwd":
            out.write(cwd + CRLF)
        elif word == "echo":
            out.write(rest + CRLF)
        elif word == "hush":
            out.write(OUTPUT_ONLY + CRLF)
        elif word == "sleep":
            await asyncio.sleep(float(rest or 1))
        elif word == "exit":
            process.exit(int(rest or 0))
            return
        elif word:
            out.write(f"{word}: command not found{CRLF}")
        out.write("$ ")
    process.exit(0)


async def _fake_shell(owner: SshTestServer, process: asyncssh.SSHServerProcess[str]) -> None:
    owner.pty_commands.append(process.command)
    owner.shells.append(process)
    try:
        await _fake_shell_loop(owner, process)
    finally:
        owner.shells.remove(process)
