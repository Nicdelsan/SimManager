"""Mutating actions from the UI (M4): only "cancel job", with a server-enforced confirmation.

POST /api/actions/job-cancel/prepare   {cluster_id, job_id} -> {action_id, job details}
POST /api/actions/{action_id}/confirm  {} -> runs `scancel` via `run_mutating`

`prepare` changes nothing: it checks that the job is one of the user's active jobs known
to SimManager and returns a single-use `action_id` (valid `ACTION_TTL_S`) that the
confirmation dialog (job id, name, cluster) sends back. Without both steps nothing runs.
The transport writes `action.executed` / `action.result` to the durable event log.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import monitor as monitor_contract
from simmanager.contracts.scheduler import SERVICE_PREFIX, Scheduler
from simmanager.contracts.transport import (
    ActionApproval,
    ClusterUnavailableError,
    TransportError,
)

from .api import Api

ACTION_TTL_S = 60.0
CANCEL_ACTION = "job.cancel"


@dataclass(frozen=True)
class PendingCancel:
    cluster_id: str
    job_id: str
    name: str
    expires_at: float


@dataclass
class PendingActions:
    ttl_s: float = ACTION_TTL_S
    clock: Callable[[], float] = time.monotonic
    _pending: dict[str, PendingCancel] = field(default_factory=dict)

    def add(self, cluster_id: str, job_id: str, name: str) -> str:
        now = self.clock()
        self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
        action_id = secrets.token_urlsafe(16)
        self._pending[action_id] = PendingCancel(cluster_id, job_id, name, now + self.ttl_s)
        return action_id

    def take(self, action_id: str) -> PendingCancel | None:
        """Single use: the id is gone after the first call, whatever happens next."""
        action = self._pending.pop(action_id, None)
        if action is None or action.expires_at <= self.clock():
            return None
        return action


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str = Field(min_length=1, max_length=200)
    job_id: str = Field(min_length=1, max_length=100, pattern=r"^[0-9][0-9_.+\[\]-]*$")


def build_actions_router(api: Api, pending: PendingActions | None = None) -> APIRouter:
    r = APIRouter()
    actions = pending or PendingActions()

    @r.post("/actions/job-cancel/prepare")
    async def prepare_cancel(body: CancelBody) -> dict[str, Any]:
        if body.cluster_id not in api.transport.clusters():
            raise HTTPException(404, "unknown cluster")
        jobs = await api.storage.jobs(cluster_id=body.cluster_id, active=True, limit=2000)
        job = next((j for j in jobs if j.info.job_id == body.job_id), None)
        if job is None:
            raise HTTPException(404, "not an active job of yours known to SimManager")
        action_id = actions.add(body.cluster_id, body.job_id, job.info.name)
        return {
            "action_id": action_id,
            "action": CANCEL_ACTION,
            "cluster_id": body.cluster_id,
            "job_id": body.job_id,
            "name": job.info.name,
            "state": str(job.info.state),
            "work_dir": job.info.work_dir,
            "expires_in_s": actions.ttl_s,
        }

    @r.post("/actions/{action_id}/confirm")
    async def confirm(action_id: str) -> dict[str, Any]:
        action = actions.take(action_id)
        if action is None:
            raise HTTPException(404, "unknown or expired confirmation: start again")
        transport = api.transport
        profile = transport.clusters().get(action.cluster_id)
        scheduler: Scheduler | None = (
            api.services.get(SERVICE_PREFIX + profile.scheduler) if profile else None
        )
        if profile is None or scheduler is None:
            raise HTTPException(503, "scheduler not available")
        try:
            session = await transport.session(action.cluster_id)
            result = await session.run_mutating(
                scheduler.cancel_argv(action.job_id),
                approval=ActionApproval(
                    action=CANCEL_ACTION,
                    approved_by="web",
                    reason=f"job {action.job_id} ({action.name}) confirmed in the web UI",
                ),
            )
        except ClusterUnavailableError as exc:
            raise HTTPException(409, f"{action.cluster_id} is {exc.state}: {exc.detail}") from None
        except TransportError as exc:
            raise HTTPException(502, str(exc)) from None

        monitor = api.services.get(monitor_contract.SERVICE_KEY)
        if monitor is not None:  # show the new job state soon, not at the next poll
            api.spawn(_poll_soon(monitor, action.cluster_id), f"poll-after-cancel-{action.job_id}")
        return {
            "cluster_id": action.cluster_id,
            "job_id": action.job_id,
            "exit_status": result.exit_status,
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
        }

    return r


async def _poll_soon(monitor: Any, cluster_id: str) -> None:
    await asyncio.sleep(2)  # let Slurm register the cancellation
    with contextlib.suppress(Exception):  # the regular poll loop reports problems
        await monitor.poll(cluster_id)
