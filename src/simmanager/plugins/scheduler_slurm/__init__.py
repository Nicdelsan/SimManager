"""`scheduler.slurm` service: SLURM queries over a RemoteSession."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import tzinfo

from simmanager.contracts import scheduler as contract
from simmanager.contracts.scheduler import JobInfo
from simmanager.contracts.transport import RemoteSession, TransportError
from simmanager.kernel import Context, Plugin

from .parse import (
    SACCT_DISABLED,
    parse_sacct,
    parse_scontrol,
    parse_squeue,
    sacct_argv,
    scontrol_argv,
    squeue_argv,
)

log = logging.getLogger("simmanager.plugin.scheduler-slurm")

SACCT_BATCH = 100


class SlurmScheduler:
    """Implements `simmanager.contracts.scheduler.Scheduler`."""

    kind = "slurm"

    def __init__(self) -> None:
        self._accounting: dict[str, bool] = {}
        """Per cluster: False once sacct reported that accounting is disabled."""

    def accounting(self, cluster_id: str) -> bool | None:
        """True/False once known for the cluster, None before the first sacct call."""
        return self._accounting.get(cluster_id)

    async def list_active(
        self, session: RemoteSession, user: str, tz: tzinfo | None
    ) -> list[JobInfo]:
        result = await session.run_readonly(squeue_argv(user), timeout_s=60)
        if not result.ok:
            raise TransportError(f"squeue failed ({result.exit_status}): {result.stderr.strip()}")
        return parse_squeue(result.stdout, tz)

    async def final_states(
        self, session: RemoteSession, job_ids: Sequence[str], tz: tzinfo | None
    ) -> dict[str, JobInfo]:
        wanted = list(dict.fromkeys(job_ids))
        found: dict[str, JobInfo] = {}
        cid = session.cluster_id
        if self._accounting.get(cid, True):
            for i in range(0, len(wanted), SACCT_BATCH):
                batch = wanted[i : i + SACCT_BATCH]
                result = await session.run_readonly(sacct_argv(batch), timeout_s=60)
                if SACCT_DISABLED in (result.stdout + result.stderr).lower():
                    log.info("%s: SLURM accounting disabled, using scontrol", cid)
                    self._accounting[cid] = False
                    break
                if not result.ok:
                    log.warning("%s: sacct failed: %s", cid, result.stderr.strip()[:300])
                    break
                self._accounting[cid] = True
                found.update((j.job_id, j) for j in parse_sacct(result.stdout, tz))

        # ASSUMPTION(A3): without accounting, scontrol still knows a job for ~MinJobAge.
        for job_id in wanted:
            if job_id in found:
                continue
            result = await session.run_readonly(scontrol_argv(job_id))
            if result.ok:
                found.update((j.job_id, j) for j in parse_scontrol(result.stdout, tz))
        return {jid: found[jid] for jid in wanted if jid in found}

    def cancel_argv(self, job_id: str) -> list[str]:
        return ["scancel", job_id]


class SlurmSchedulerPlugin(Plugin):
    name = "scheduler-slurm"
    provides = contract.SERVICE_PREFIX + "slurm"
    contract_version = contract.CONTRACT_VERSION
    description = "SLURM: squeue for active jobs, sacct or scontrol for final states."

    def apply(self, ctx: Context) -> SlurmScheduler:
        return SlurmScheduler()


__all__ = ["SlurmScheduler", "SlurmSchedulerPlugin"]
