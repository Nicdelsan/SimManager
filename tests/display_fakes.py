"""Fake cluster for display tests: a local folder plus a `run_mutating` that pretends the
script wrote some images (nothing is executed)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from simmanager.contracts.clusters import ClusterProfile
from simmanager.contracts.transport import ActionApproval, CommandResult, ConnectionState
from simmanager.kernel import Plugin
from simmanager.readers import LocalRemoteSession

C = "c1"
WORK = "/work/user1/case_a"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


@dataclass
class Call:
    argv: list[str]
    input: str | None
    approval: ActionApproval


class ScriptSession(LocalRemoteSession):
    """Runs nothing: pretends the script wrote `produce` and prints the listing."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, cluster_id=C, virtual_root=WORK)
        self.root = root
        self.calls: list[Call] = []
        self.produce: dict[str, bytes] = {}
        self.exit_status = 0
        self.gate: asyncio.Event | None = None

    async def run_mutating(
        self,
        argv: Sequence[str],
        *,
        approval: ActionApproval,
        timeout_s: float = 30,
        input: str | None = None,
    ) -> CommandResult:
        self.calls.append(Call(list(argv), input, approval))
        if self.gate is not None:
            await self.gate.wait()
        match = re.search(r"^echo (__SIMMAN_DISPLAY_IMAGES_\w+__)$", argv[2], re.M)
        assert match is not None
        lines = ["hello from the script", match.group(1)]
        for rel, data in self.produce.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            lines.append(f"{path.stat().st_mtime}\t{len(data)}\t{rel}")
        return CommandResult(tuple(argv), self.exit_status, "\n".join(lines) + "\n", "")


@dataclass
class FakeTransport:
    session_: ScriptSession
    profiles: dict[str, ClusterProfile] = field(
        default_factory=lambda: {C: ClusterProfile(host="cluster.example.org", user="user1")}
    )

    def clusters(self) -> Mapping[str, ClusterProfile]:
        return self.profiles

    async def session(self, cluster_id: str) -> ScriptSession:
        return self.session_

    def state(self, cluster_id: str) -> ConnectionState:
        return ConnectionState.CONNECTED

    async def reset(self, cluster_id: str) -> None:
        return None


def transport_plugin(transport: FakeTransport) -> type[Plugin]:
    class FakeTransportPlugin(Plugin):
        name = "fake-transport"
        provides = "transport"
        contract_version = 1

        def apply(self, ctx: object) -> FakeTransport:
            return transport

    return FakeTransportPlugin
