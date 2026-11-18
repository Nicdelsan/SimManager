"""SSH transport against an in-process asyncssh server."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from simmanager.contracts.clusters import ClusterProfile
from simmanager.contracts.transport import (
    ActionApproval,
    ClusterUnavailableError,
    CommandNotAllowedError,
    ConnectionState,
)
from simmanager.plugins.storage_sqlite import SqliteStorage
from simmanager.plugins.transport_ssh import SshTransport

from .sshserver import USER, FakePrompter, SshTestServer

C = "c1"


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[SshTestServer]:
    srv = SshTestServer(tmp_path / "ssh")
    srv.commands["hostname"] = ("login01\n", "", 0)
    await srv.start()
    yield srv
    await srv.stop()


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[SqliteStorage]:
    s = await SqliteStorage.open(str(tmp_path / "t.db"))
    yield s
    await s.close()


class Harness:
    def __init__(self, storage: SqliteStorage) -> None:
        self.storage = storage
        self.bus: list[tuple[str, Any]] = []
        self.prompter: FakePrompter | None = FakePrompter()
        self.transports: list[SshTransport] = []

    def transport(self, profile: ClusterProfile) -> SshTransport:
        async def emit(event: str, payload: Any) -> None:
            self.bus.append((event, payload))

        t = SshTransport(
            {C: profile}, storage=self.storage, emit=emit, prompter=lambda: self.prompter
        )
        self.transports.append(t)
        return t

    async def kinds(self) -> list[str]:
        return [e.kind for e in await self.storage.events()]


@pytest.fixture
async def h(storage: SqliteStorage) -> AsyncIterator[Harness]:
    harness = Harness(storage)
    yield harness
    for t in harness.transports:
        await t.close()


async def test_key_auth_and_readonly_command(server: SshTestServer, h: Harness) -> None:
    key = server.new_client_key("id_test")
    t = h.transport(server.profile(key_files=[str(key)]))
    assert t.state(C) is ConnectionState.DISCONNECTED

    session = await t.session(C)
    result = await session.run_readonly(["hostname"])
    assert result.ok and result.stdout == "login01\n"
    assert session.user == USER
    assert t.state(C) is ConnectionState.CONNECTED
    assert "cluster.connected" in await h.kinds()
    assert (
        "transport.state",
        {"cluster_id": C, "state": "connected", "detail": f"as {USER}"},
    ) in h.bus

    # one shared connection
    again = await t.session(C)
    assert (await again.run_readonly(["hostname"])).ok
    assert server.connections == 1


async def test_arguments_are_quoted_and_policy_enforced(server: SshTestServer, h: Harness) -> None:
    t = h.transport(server.profile(key_files=[str(server.new_client_key("id_test"))]))
    session = await t.session(C)
    await session.run_readonly(["ls", "-l", "/a dir/$HOME;rm x"])
    assert server.executed[-1] == "ls -l '/a dir/$HOME;rm x'"

    for argv in (["rm", "-rf", "/"], ["scontrol", "update", "x"], ["find", ".", "-delete"]):
        with pytest.raises(CommandNotAllowedError):
            await session.run_readonly(argv)
    assert all(not c.startswith(("rm", "scontrol update", "find")) for c in server.executed)


async def test_mutating_command_is_logged(server: SshTestServer, h: Harness) -> None:
    server.commands["scancel 42"] = ("", "", 0)
    t = h.transport(server.profile(key_files=[str(server.new_client_key("id_test"))]))
    session = await t.session(C)
    approval = ActionApproval(action="job.cancel", approved_by="test", reason="unit test")
    result = await session.run_mutating(["scancel", "42"], approval=approval)
    assert result.ok and server.executed[-1] == "scancel 42"
    events = {e.kind: e for e in await h.storage.events()}
    assert events["action.executed"].payload["argv"] == ["scancel", "42"]
    assert events["action.result"].payload["exit_status"] == 0


async def test_mutating_command_input_goes_to_stdin_not_to_the_log(
    server: SshTestServer, h: Harness
) -> None:
    server.commands["bash -lc 'cat > /tmp/x'"] = ("", "", 0)
    server.read_stdin.add("bash -lc 'cat > /tmp/x'")
    t = h.transport(server.profile(key_files=[str(server.new_client_key("id_test"))]))
    session = await t.session(C)
    approval = ActionApproval(action="display.run_script", approved_by="test")
    script = "print('secret plot')\n"
    result = await session.run_mutating(
        ["bash", "-lc", "cat > /tmp/x"], approval=approval, input=script
    )
    assert result.ok and server.stdin["bash -lc 'cat > /tmp/x'"] == script
    payloads = [str(e.payload) for e in await h.storage.events()]
    assert not any("secret plot" in p for p in payloads)


async def test_encrypted_key_asks_passphrase(server: SshTestServer, h: Harness) -> None:
    key = server.new_client_key("id_enc", passphrase="s3cret")
    assert h.prompter is not None
    h.prompter.answers = [["wrong"], ["s3cret"]]
    t = h.transport(server.profile(key_files=[str(key)]))
    await t.session(C)
    assert [r.kind for r in h.prompter.asked] == ["passphrase", "passphrase"]
    assert "wrong" in h.prompter.asked[1].title


async def test_passphrase_is_not_kept_for_reconnections(server: SshTestServer, h: Harness) -> None:
    key = server.new_client_key("id_enc", passphrase="s3cret")
    assert h.prompter is not None
    h.prompter.answers = [["s3cret"], ["s3cret"]]
    t = h.transport(server.profile(key_files=[str(key)]))
    await t.session(C)
    await t.reset(C)  # drops the connection, as a network loss would
    await t.session(C)
    assert [r.kind for r in h.prompter.asked] == ["passphrase", "passphrase"]
    assert not any("s3cret" in repr(v) for v in vars(t._clusters[C]).values())


async def test_encrypted_key_without_prompter_awaits_auth(
    server: SshTestServer, h: Harness
) -> None:
    key = server.new_client_key("id_enc", passphrase="s3cret")
    h.prompter = None
    t = h.transport(server.profile(key_files=[str(key)]))
    with pytest.raises(ClusterUnavailableError) as err:
        await t.session(C)
    assert err.value.state is ConnectionState.AWAITING_AUTH
    assert "cluster.auth_required" in await h.kinds()


async def test_password_auth_and_no_blind_retry(server: SshTestServer, h: Harness) -> None:
    server.password = "pw"
    assert h.prompter is not None
    h.prompter.answers = [["bad"]]
    t = h.transport(server.profile(auth=["password"]))
    with pytest.raises(ClusterUnavailableError) as err:
        await t.session(C)
    assert err.value.state is ConnectionState.AWAITING_AUTH
    attempts = server.auth_attempts

    with pytest.raises(ClusterUnavailableError):  # not retried by itself
        await t.session(C)
    assert server.auth_attempts == attempts

    h.prompter.answers = [["pw"]]
    await t.reset(C)
    await t.session(C)
    assert t.state(C) is ConnectionState.CONNECTED


async def test_keyboard_interactive_otp(server: SshTestServer, h: Harness) -> None:
    server.otp = "123456"
    assert h.prompter is not None
    h.prompter.answers = [["123456"]]
    t = h.transport(server.profile(auth=["keyboard-interactive"]))
    await t.session(C)
    request = h.prompter.asked[0]
    assert request.kind == "keyboard-interactive"
    assert request.instructions == "Enter the code from your app"
    assert [(p.text, p.echo) for p in request.prompts] == [("Code: ", False)]


async def test_valid_certificate(server: SshTestServer, h: Harness) -> None:
    key, _ = server.new_cert_key("id_cert", valid_for=timedelta(hours=12))
    t = h.transport(server.profile(key_files=[str(key)]))
    await t.session(C)
    assert t.state(C) is ConnectionState.CONNECTED


async def test_expired_certificate_without_renew_command(server: SshTestServer, h: Harness) -> None:
    key, _ = server.new_cert_key("id_cert", valid_for=-timedelta(minutes=5))
    t = h.transport(server.profile(key_files=[str(key)]))
    with pytest.raises(ClusterUnavailableError, match="certificate expired") as err:
        await t.session(C)
    assert err.value.state is ConnectionState.AWAITING_AUTH
    assert server.auth_attempts == 0  # never offered an expired certificate


async def test_expired_certificate_renewed_after_confirmation(
    server: SshTestServer, h: Harness, tmp_path: Path
) -> None:
    key, ca = server.new_cert_key("id_cert", valid_for=-timedelta(minutes=5))
    fresh = tmp_path / "fresh"
    SshTestServer.write_cert(ca, key, fresh, timedelta(hours=12))
    copy = "import shutil,sys; shutil.copyfile(sys.argv[1], sys.argv[2])"
    renew = [sys.executable, "-c", copy, f"{fresh}-cert.pub", f"{key}-cert.pub"]
    assert h.prompter is not None
    h.prompter.confirm_answer = True
    t = h.transport(server.profile(key_files=[str(key)], cert_renew_command=renew))
    await t.session(C)
    assert "expired" in h.prompter.questions[0]
    kinds = await h.kinds()
    assert {"cluster.cert_renew_started", "cluster.cert_renew_finished"} <= set(kinds)


async def test_certificate_renewed_outside_is_picked_up(server: SshTestServer, h: Harness) -> None:
    key, ca = server.new_cert_key("id_cert", valid_for=-timedelta(minutes=5))
    t = h.transport(server.profile(key_files=[str(key)]))
    with pytest.raises(ClusterUnavailableError):
        await t.session(C)
    SshTestServer.write_cert(ca, key, key, timedelta(hours=12))  # e.g. the user's own script
    await t.session(C)
    assert t.state(C) is ConnectionState.CONNECTED


async def test_unreachable_cluster_backs_off(h: Harness, tmp_path: Path) -> None:
    profile = ClusterProfile(
        host="127.0.0.1",
        port=1,
        user=USER,
        ssh_config_file=str(tmp_path / "none"),
        connect_timeout_s=2,
    )
    (tmp_path / "none").write_text("")
    t = h.transport(profile)
    with pytest.raises(ClusterUnavailableError) as err:
        await t.session(C)
    assert err.value.state is ConnectionState.BACKOFF and err.value.retry_at is not None
    with pytest.raises(ClusterUnavailableError) as again:  # still backing off: no new attempt
        await t.session(C)
    assert again.value.retry_at == err.value.retry_at
    assert (await h.kinds()).count("cluster.unreachable") == 1


async def test_unknown_host_key_fails(server: SshTestServer, h: Harness) -> None:
    server.known_hosts.write_text("", encoding="utf-8")
    t = h.transport(server.profile(key_files=[str(server.new_client_key("id_test"))]))
    with pytest.raises(ClusterUnavailableError, match="host key") as err:
        await t.session(C)
    assert err.value.state is ConnectionState.FAILED


async def test_reconnects_after_server_restart(server: SshTestServer, h: Harness) -> None:
    t = h.transport(server.profile(key_files=[str(server.new_client_key("id_test"))]))
    await t.session(C)
    await server.stop()
    for _ in range(200):
        if t.state(C) is ConnectionState.DISCONNECTED:
            break
        await asyncio.sleep(0.01)
    assert t.state(C) is ConnectionState.DISCONNECTED
    await server.start()
    session = await t.session(C)
    assert (await session.run_readonly(["hostname"])).ok
    assert "cluster.connection_lost" in await h.kinds()


async def test_sftp_reads(server: SshTestServer, h: Harness) -> None:
    case = server.sftp_root / "case"
    case.mkdir()
    (case / "log.txt").write_bytes(b"0123456789")
    t = h.transport(server.profile(key_files=[str(server.new_client_key("id_test"))]))
    session = await t.session(C)

    st = await session.stat("/case/log.txt")
    assert st is not None and st.size == 10 and not st.is_dir
    assert await session.stat("/case/missing") is None
    listing = await session.listdir("/case")
    assert [s.path for s in listing] == ["/case/log.txt"]
    assert await session.read_range("/case/log.txt", 3, 4) == b"3456"
    assert await session.read_range("/case/log.txt", 8, 100) == b"89"


async def test_windows_path_in_ssh_config_gets_a_hint(server: SshTestServer, h: Harness) -> None:
    key = server.new_client_key("id_test")
    windows_path = str(key).replace("/", "\\")
    server.ssh_config.write_text(
        f"Host alias\n  HostName 127.0.0.1\n  IdentityFile {windows_path}\n", encoding="utf-8"
    )
    t = h.transport(server.profile(host=None, ssh_config_host="alias"))
    with pytest.raises(ClusterUnavailableError) as err:
        await t.session(C)
    if "C:" in windows_path:  # the mangling only happens to drive-letter paths
        assert "forward slashes" in err.value.detail
    assert err.value.state is ConnectionState.FAILED
