"""`auth.prompter` in the browser: passwords, OTP codes and confirmations over the
dashboard WebSocket.

Every connected browser gets the prompt (late browsers too, on connect); the first answer
wins and the others get `auth.closed`. No answer within `timeout_s` (also when no browser
is open) -> None/False: the transport goes to AWAITING_AUTH and does not retry blindly.

Secrets: answers arrive only over the authenticated WebSocket (never in a URL or a REST
body), are handed to the transport and dropped; they are never logged, stored or put in
an event. Python cannot wipe strings from memory; no reference is kept after use.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from simmanager.contracts.auth import AuthRequest
from simmanager.contracts.ui import Message, UiWeb

MAX_ANSWER_LEN = 4096


@dataclass
class _Pending:
    message: dict[str, Any]
    future: asyncio.Future[Any]
    n_prompts: int  # 0 for a confirmation


class WebPrompter:
    """Implements `simmanager.contracts.auth.AuthPrompter`."""

    def __init__(self, ui: UiWeb, *, timeout_s: float, log: logging.Logger) -> None:
        self._ui = ui
        self._timeout_s = timeout_s
        self._log = log
        self._pending: dict[str, _Pending] = {}

    def install(self) -> list[Callable[[], None]]:
        return [
            self._ui.on_message("auth.answer", self._on_answer),
            self._ui.on_message("auth.cancel", self._on_cancel),
            self._ui.on_message("auth.confirm_answer", self._on_confirm_answer),
            self._ui.on_client_connected(self._on_client_connected),
        ]

    # ---------------------------------------------------------------- contract
    async def ask(self, request: AuthRequest) -> list[str] | None:
        message = {
            "type": "auth.prompt",
            "cluster_id": request.cluster_id,
            "kind": request.kind,
            "title": request.title,
            "instructions": request.instructions,
            "prompts": [{"text": p.text, "echo": p.echo} for p in request.prompts],
        }
        answer = await self._wait(message, n_prompts=len(request.prompts))
        return answer if isinstance(answer, list) else None

    async def confirm(self, cluster_id: str, question: str) -> bool:
        message = {"type": "auth.confirm", "cluster_id": cluster_id, "question": question}
        return await self._wait(message, n_prompts=0) is True

    def pending_count(self) -> int:
        return len(self._pending)

    async def close(self) -> None:
        """Plugin unloading: unanswered prompts end as 'no answer'."""
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(None)

    # ---------------------------------------------------------------- internals
    async def _wait(self, message: dict[str, Any], *, n_prompts: int) -> Any:
        prompt_id = secrets.token_urlsafe(12)
        message["prompt_id"] = prompt_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[prompt_id] = _Pending(message, future, n_prompts)
        self._log.warning(
            "%s needs authentication: answer in the web UI (%s)",
            message["cluster_id"],
            self._ui.base_url,
        )
        try:
            await self._ui.broadcast(message)
            async with asyncio.timeout(self._timeout_s):
                return await future
        except TimeoutError:
            self._log.warning("%s: authentication prompt timed out", message["cluster_id"])
            return None
        finally:
            del self._pending[prompt_id]
            await self._ui.broadcast({"type": "auth.closed", "prompt_id": prompt_id})

    def _take(self, message: Message, *, confirmation: bool) -> _Pending | None:
        prompt_id = message.get("prompt_id")
        pending = self._pending.get(prompt_id) if isinstance(prompt_id, str) else None
        if pending is None or pending.future.done():
            return None  # unknown, expired or already answered by another browser
        if (pending.n_prompts == 0) != confirmation:
            return None
        return pending

    async def _on_answer(self, message: Message) -> None:
        pending = self._take(message, confirmation=False)
        if pending is None:
            return
        values = message.get("values")
        if (
            not isinstance(values, list)
            or len(values) != pending.n_prompts
            or not all(isinstance(v, str) and len(v) <= MAX_ANSWER_LEN for v in values)
        ):
            return  # malformed: keep waiting (never echo or log the values)
        pending.future.set_result(list(values))

    async def _on_cancel(self, message: Message) -> None:
        pending = self._take(message, confirmation=False) or self._take(message, confirmation=True)
        if pending is not None:
            pending.future.set_result(None)

    async def _on_confirm_answer(self, message: Message) -> None:
        pending = self._take(message, confirmation=True)
        if pending is not None and isinstance(message.get("yes"), bool):
            pending.future.set_result(message["yes"])

    async def _on_client_connected(self) -> list[Message]:
        return [p.message for p in self._pending.values() if not p.future.done()]
