"""`auth.prompter` contract: ask the human for credentials or a confirmation.

Implemented by `prompter-cli` (terminal) and later by the web UI. Answers are never stored
on disk by the transport.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

SERVICE_KEY = "auth.prompter"
CONTRACT_VERSION = 1


@dataclass(frozen=True)
class Prompt:
    text: str
    echo: bool = False
    """True when the answer may be shown while typing (e.g. a user name)."""


@dataclass(frozen=True)
class AuthRequest:
    cluster_id: str
    kind: Literal["password", "passphrase", "keyboard-interactive"]
    title: str
    instructions: str = ""
    prompts: Sequence[Prompt] = field(default_factory=tuple)


class AuthPrompter(Protocol):
    async def ask(self, request: AuthRequest) -> list[str] | None:
        """One answer per prompt, or None if the user cancelled or nobody can answer."""
        ...

    async def confirm(self, cluster_id: str, question: str) -> bool:
        """Yes/no question. False when nobody can answer."""
        ...
