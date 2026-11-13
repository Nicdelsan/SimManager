"""In-process SSH server (asyncssh) for transport tests. Everything is synthetic."""

from __future__ import annotations

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
        command = str(process.command or "")
        self.executed.append(command)
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
