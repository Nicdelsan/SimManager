"""`auth.prompter` service on the terminal (getpass / input).

When stdin is not an interactive terminal (service mode, tests, pipes) it answers None /
False immediately, so the transport goes to AWAITING_AUTH instead of hanging.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from collections.abc import Callable

from simmanager.contracts import auth as contract
from simmanager.contracts.auth import AuthRequest
from simmanager.kernel import Context, Plugin


class CliPrompter:
    """Implements `simmanager.contracts.auth.AuthPrompter`."""

    def __init__(self, interactive: Callable[[], bool] | None = None) -> None:
        self._interactive = interactive or (lambda: sys.stdin is not None and sys.stdin.isatty())
        self._lock = asyncio.Lock()  # one question at a time on the terminal

    async def ask(self, request: AuthRequest) -> list[str] | None:
        if not self._interactive():
            return None
        async with self._lock:
            return await asyncio.to_thread(self._ask_blocking, request)

    async def confirm(self, cluster_id: str, question: str) -> bool:
        if not self._interactive():
            return False
        async with self._lock:
            answer = await asyncio.to_thread(input, f"[{cluster_id}] {question} [y/N] ")
        return answer.strip().lower() in {"y", "yes", "s", "si", "sì"}

    @staticmethod
    def _ask_blocking(request: AuthRequest) -> list[str] | None:
        print(f"\n[{request.cluster_id}] {request.title}", file=sys.stderr)
        if request.instructions:
            print(request.instructions, file=sys.stderr)
        try:
            return [input(p.text) if p.echo else getpass.getpass(p.text) for p in request.prompts]
        except (EOFError, KeyboardInterrupt):
            return None


class CliPrompterPlugin(Plugin):
    name = "prompter-cli"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    description = "Asks passwords, OTP codes and confirmations on the terminal."

    def apply(self, ctx: Context) -> CliPrompter:
        return CliPrompter()


__all__ = ["CliPrompter", "CliPrompterPlugin"]
