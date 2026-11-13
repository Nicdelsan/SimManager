"""`monitor` service: discovers the user's jobs on every cluster and records them.

One poll loop per cluster with an adaptive interval (running / queued / idle, see
`PollConfig`). Jobs and runs are durable facts in storage: unloading this plugin only stops
the loops.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import monitor as contract
from simmanager.contracts.monitor import PollReport
from simmanager.contracts.scheduler import SERVICE_PREFIX, JobInfo, JobState, Scheduler
from simmanager.contracts.storage import JobRecord, Storage
from simmanager.contracts.transport import ClusterUnavailableError, Transport, TransportError
from simmanager.kernel import Context, Plugin, ServiceView

log = logging.getLogger("simmanager.plugin.monitor")

SOURCE = "monitor"
Emit = Callable[[str, Any], Awaitable[None]]


class MonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loop: bool = True
    """Poll in the background. The CLI turns it off for one-shot commands."""
    clusters: list[str] | None = None
    """Cluster ids to monitor. Default: all clusters in clusters.yaml."""
    failure_threshold: int = Field(default=3, ge=1)
    """Consecutive failed polls before `cluster.degraded` is recorded."""


class JobMonitor:
    """Implements `simmanager.contracts.monitor.Monitor`."""

    def __init__(
        self,
        *,
        storage: Storage,
        transport: Transport,
        services: ServiceView,
        emit: Emit,
        config: MonitorConfig,
    ) -> None:
        self._storage = storage
        self._transport = transport
        self._services = services
        self._emit = emit
        self._config = config
        self._failures: dict[str, int] = {}
        self._degraded: set[str] = set()

    def cluster_ids(self) -> list[str]:
        known = list(self._transport.clusters())
        wanted = self._config.clusters
        return known if wanted is None else [c for c in known if c in wanted]

    async def poll_all(self) -> list[PollReport]:
        return list(await asyncio.gather(*(self.poll(cid) for cid in self.cluster_ids())))

    async def run_loop(self, cluster_id: str) -> None:
        while True:
            report = await self.poll(cluster_id)
            await asyncio.sleep(report.next_poll_s)

    async def poll(self, cluster_id: str) -> PollReport:
        profile = self._transport.clusters()[cluster_id]
        poll = profile.poll
        try:
            report = await self._poll(cluster_id)
        except ClusterUnavailableError as exc:
            wait = poll.idle_s
            if exc.retry_at is not None:
                until_retry = (exc.retry_at - datetime.now(UTC)).total_seconds()
                wait = min(poll.idle_s, max(until_retry, 1.0))
            report = PollReport(cluster_id, ok=False, error=str(exc), next_poll_s=wait)
        except TransportError as exc:
            await self._failed(cluster_id, str(exc))
            report = PollReport(cluster_id, ok=False, error=str(exc), next_poll_s=poll.queued_s)
        except Exception as exc:  # a bug must not stop the loop: record it and go on
            log.exception("%s: poll failed", cluster_id)
            await self._failed(cluster_id, f"{type(exc).__name__}: {exc}")
            report = PollReport(
                cluster_id, ok=False, error=f"internal error: {exc}", next_poll_s=poll.idle_s
            )
        else:
            await self._recovered(cluster_id)
        jitter = 1 + random.uniform(-poll.jitter, poll.jitter)
        report = replace(report, next_poll_s=report.next_poll_s * jitter)
        await self._emit(contract.POLLED_EVENT, report)
        return report

    async def _poll(self, cluster_id: str) -> PollReport:
        profile = self._transport.clusters()[cluster_id]
        scheduler: Scheduler | None = self._services.get(SERVICE_PREFIX + profile.scheduler)
        if scheduler is None:
            raise TransportError(f"no scheduler plugin for {profile.scheduler!r}")
        tz = profile.tz()
        session = await self._transport.session(cluster_id)
        active = await scheduler.list_active(session, session.user, tz)

        known = {
            r.info.job_id: r
            for r in await self._storage.jobs(cluster_id=cluster_id, active=True, limit=1_000_000)
        }
        discovered = changed = 0
        for info in active:
            created, did_change = await self._record(cluster_id, info, active=True)
            discovered += created
            changed += did_change

        seen = {j.job_id for j in active}
        gone = sorted(jid for jid in known if jid not in seen)
        finals = await scheduler.final_states(session, gone, tz) if gone else {}
        for jid in gone:
            info = finals.get(jid) or _unknown_end(known[jid])
            # Not terminal (e.g. a requeue seen between two commands): check again next poll.
            _, did_change = await self._record(cluster_id, info, active=not info.state.terminal)
            changed += did_change

        poll = profile.poll
        states = {j.state for j in active}
        if states & {JobState.RUNNING, JobState.COMPLETING}:
            interval = poll.running_s
        elif states:
            interval = poll.queued_s
        else:
            interval = poll.idle_s
        return PollReport(
            cluster_id,
            ok=True,
            active_jobs=len(active),
            discovered=discovered,
            changed=changed,
            next_poll_s=interval,
        )

    async def _record(self, cluster_id: str, info: JobInfo, *, active: bool) -> tuple[bool, bool]:
        up = await self._storage.upsert_job(cluster_id, info, active=active)
        run_id = up.record.run_id
        if up.created:
            await self._event(
                "job.discovered",
                cluster_id,
                run_id,
                {
                    "job_id": info.job_id,
                    "name": info.name,
                    "state": str(info.state),
                    "work_dir": info.work_dir,
                },
            )
            await self._event(
                "run.created",
                cluster_id,
                run_id,
                {"job_id": info.job_id, "origin": "discovered", "work_dir": info.work_dir},
            )
            return True, False
        assert up.previous is not None
        if up.previous.state is info.state:
            return False, False
        await self._event(
            "job.state_changed",
            cluster_id,
            run_id,
            {
                "job_id": info.job_id,
                "from": str(up.previous.state),
                "to": str(info.state),
                "raw_state": info.raw_state,
                "reason": info.reason,
                "exit_code": info.exit_code,
            },
        )
        return False, True

    async def _event(
        self, kind: str, cluster_id: str, run_id: int | None, payload: Mapping[str, Any]
    ) -> None:
        await self._storage.append_event(
            kind, payload, source=SOURCE, cluster_id=cluster_id, run_id=run_id
        )

    async def _failed(self, cluster_id: str, error: str) -> None:
        n = self._failures[cluster_id] = self._failures.get(cluster_id, 0) + 1
        log.warning("%s: poll failed (%d in a row): %s", cluster_id, n, error)
        if n >= self._config.failure_threshold and cluster_id not in self._degraded:
            self._degraded.add(cluster_id)
            await self._event(
                "cluster.degraded", cluster_id, None, {"failures": n, "last_error": error}
            )

    async def _recovered(self, cluster_id: str) -> None:
        self._failures[cluster_id] = 0
        if cluster_id in self._degraded:
            self._degraded.discard(cluster_id)
            await self._event("cluster.recovered", cluster_id, None, {})


def _unknown_end(record: JobRecord) -> JobInfo:
    """A job left the queue and neither sacct nor scontrol knows it any more."""
    return replace(
        record.info,
        state=JobState.UNKNOWN,
        reason="left the queue; final state not available from the scheduler",
    )


class MonitorPlugin(Plugin):
    name = "monitor"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {"storage": 1, "transport": 1}
    config_schema = MonitorConfig
    description = "Polls each cluster's scheduler and records jobs, runs and state changes."

    def apply(self, ctx: Context) -> JobMonitor:
        assert isinstance(ctx.config, MonitorConfig)
        monitor = JobMonitor(
            storage=ctx.services.require("storage"),
            transport=ctx.services.require("transport"),
            services=ctx.services,
            emit=ctx.emit,
            config=ctx.config,
        )
        if ctx.config.loop:
            for cid in monitor.cluster_ids():
                ctx.task(monitor.run_loop(cid), name=f"poll:{cid}")
        return monitor


__all__ = ["JobMonitor", "MonitorConfig", "MonitorPlugin", "PollReport"]
