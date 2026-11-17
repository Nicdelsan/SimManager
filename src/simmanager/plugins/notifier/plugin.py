"""`notifier` router plugin: event routing, immediate-first batching, quiet hours, rate limit."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime
from datetime import time as dt_time
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from simmanager.contracts import notifier as contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts.notifier import (
    NotificationMessage,
    NotificationSeverity,
    NotifierProvider,
)
from simmanager.contracts.storage import EventRecord
from simmanager.kernel import Context, Plugin

_QUIET_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")
_SEVERITY_RANK = {
    NotificationSeverity.INFO: 0,
    NotificationSeverity.WARNING: 1,
    NotificationSeverity.CRITICAL: 2,
}


class NotifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled_events: list[str] = Field(
        default_factory=lambda: ["alert.raised", "job.state_changed", "cluster.auth_required"]
    )
    min_alert_severity: NotificationSeverity = NotificationSeverity.WARNING
    notify_job_states: list[str] = Field(default_factory=lambda: ["completed", "failed", "timeout"])
    batch_window_s: float = Field(default=15.0, ge=0.0)
    max_per_minute: int = Field(default=6, ge=1)
    quiet_hours: str | None = None
    """Optional local quiet hours window `HH:MM-HH:MM`, e.g. `22:00-07:00`."""
    quiet_bypass_critical: bool = False

    @field_validator("quiet_hours")
    @classmethod
    def _validate_quiet_hours(cls, val: str | None) -> str | None:
        if val is not None:
            parse_quiet_hours(val)
        return val


def parse_quiet_hours(spec: str) -> tuple[dt_time, dt_time]:
    m = _QUIET_RE.match(spec.strip())
    if not m:
        raise ValueError(f"quiet_hours must be 'HH:MM-HH:MM', got {spec!r}")
    sh, sm, eh, em = (int(g) for g in m.groups())
    return dt_time(sh, sm), dt_time(eh, em)


def in_quiet_hours(now_local: datetime, spec: str | None) -> bool:
    if not spec:
        return False
    start, end = parse_quiet_hours(spec)
    cur = now_local.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


def format_event_notification(
    event: EventRecord,
    cfg: NotifierConfig,
) -> NotificationMessage | None:
    """Convert a durable `EventRecord` into a secret-free `NotificationMessage`, or None."""
    if event.kind not in cfg.enabled_events:
        return None

    if event.kind == "alert.raised":
        raw_sev = str(event.payload.get("severity", "warning"))
        try:
            sev = NotificationSeverity(raw_sev)
        except ValueError:
            sev = NotificationSeverity.WARNING
        if _SEVERITY_RANK[sev] < _SEVERITY_RANK[cfg.min_alert_severity]:
            return None
        title = str(event.payload.get("title") or f"Alert ({event.payload.get('rule_key', '?')})")
        body = str(event.payload.get("message") or title)
        if event.cluster_id:
            body = f"[{event.cluster_id}] {body}"
        raw_aid = event.payload.get("alert_id")
        alert_id = int(raw_aid) if isinstance(raw_aid, int) else None
        return NotificationMessage(
            title=title,
            body=body,
            severity=sev,
            category="alert",
            cluster_id=event.cluster_id,
            run_id=event.run_id,
            alert_id=alert_id,
        )

    if event.kind == "job.state_changed":
        to_state = str(event.payload.get("to", ""))
        if to_state not in cfg.notify_job_states:
            return None
        job_id = str(event.payload.get("job_id") or "?")
        sev = (
            NotificationSeverity.CRITICAL
            if to_state in ("failed", "timeout")
            else NotificationSeverity.INFO
        )
        prefix = f"[{event.cluster_id}] " if event.cluster_id else ""
        exit_code = event.payload.get("exit_code")
        reason = event.payload.get("reason")
        extra = f" (exit {exit_code})" if exit_code else (f" ({reason})" if reason else "")
        return NotificationMessage(
            title=f"Job {job_id} {to_state}",
            body=f"{prefix}Run {event.run_id or '-'}: job {job_id} -> {to_state}{extra}",
            severity=sev,
            category="job",
            cluster_id=event.cluster_id,
            run_id=event.run_id,
        )

    if event.kind == "cluster.auth_required":
        cid = event.cluster_id or str(event.payload.get("cluster_id") or "cluster")
        # Never forward prompts, credentials, or auth details
        return NotificationMessage(
            title=f"Authentication required: {cid}",
            body=f"Cluster {cid} needs manual authentication in SimManager.",
            severity=NotificationSeverity.WARNING,
            category="auth",
            cluster_id=cid,
        )

    return None


def coalesce_batch(messages: list[NotificationMessage]) -> NotificationMessage:
    if len(messages) == 1:
        return messages[0]
    top_sev = max(messages, key=lambda m: _SEVERITY_RANK[m.severity]).severity
    lines = [f"- [{m.severity}] {m.title}" for m in messages[:5]]
    if len(messages) > 5:
        lines.append(f"... (+{len(messages) - 5} more)")
    return NotificationMessage(
        title=f"SimManager: {len(messages)} notifications",
        body="\n".join(lines),
        severity=top_sev,
        category="batch",
    )


class NotificationRouter:
    """Implements `simmanager.contracts.notifier.NotificationService`."""

    def __init__(
        self,
        ctx: Context,
        cfg: NotifierConfig,
        *,
        mono_clock: Callable[[], float] = time.monotonic,
        local_clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._ctx = ctx
        self._cfg = cfg
        self._mono_clock = mono_clock
        self._local_clock = local_clock
        self._lock = asyncio.Lock()
        self._sent_times: deque[float] = deque()
        self._window_until: float | None = None
        self._batch: list[NotificationMessage] = []
        self._flush_task: asyncio.Task[None] | None = None

    def _provider_map(self) -> dict[str, NotifierProvider]:
        raw: dict[str, Any] = self._ctx.services.all("notifier")
        return {
            key.removeprefix(contract.PROVIDER_PREFIX): svc
            for key, svc in raw.items()
            if key != contract.SERVICE_KEY and key.startswith(contract.PROVIDER_PREFIX)
        }

    def providers(self) -> list[str]:
        return sorted(self._provider_map())

    def _is_suppressed_by_quiet(self, msg: NotificationMessage) -> bool:
        if not in_quiet_hours(self._local_clock(), self._cfg.quiet_hours):
            return False
        return not (
            self._cfg.quiet_bypass_critical and msg.severity is NotificationSeverity.CRITICAL
        )

    def _allow_rate(self, now_mono: float) -> bool:
        cutoff = now_mono - 60.0
        while self._sent_times and self._sent_times[0] < cutoff:
            self._sent_times.popleft()
        if len(self._sent_times) >= self._cfg.max_per_minute:
            return False
        self._sent_times.append(now_mono)
        return True

    async def notify(self, message: NotificationMessage) -> bool:
        if self._is_suppressed_by_quiet(message):
            return False

        async with self._lock:
            now_mono = self._mono_clock()
            if self._window_until is None or now_mono >= self._window_until:
                # First notification in an idle state goes out immediately
                if not self._allow_rate(now_mono):
                    return False
                if self._cfg.batch_window_s > 0:
                    self._window_until = now_mono + self._cfg.batch_window_s
                    self._schedule_flush(self._cfg.batch_window_s)
                else:
                    self._window_until = None
                to_send = message
            else:
                self._batch.append(message)
                return True

        return await self._deliver(to_send)

    def _schedule_flush(self, delay_s: float) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()

        async def _flusher() -> None:
            try:
                await asyncio.sleep(delay_s)
                await self.flush()
            except asyncio.CancelledError:
                return

        self._flush_task = self._ctx.task(_flusher(), name="notifier-batch-flush")

    async def flush(self) -> bool:
        async with self._lock:
            self._window_until = None
            if not self._batch:
                return False
            items = list(self._batch)
            self._batch.clear()
            combined = coalesce_batch(items)
            if self._is_suppressed_by_quiet(combined):
                return False
            if not self._allow_rate(self._mono_clock()):
                return False
        return await self._deliver(combined)

    async def _deliver(self, message: NotificationMessage) -> bool:
        providers = self._provider_map()
        if not providers:
            return False
        any_ok = False
        for name, prov in providers.items():
            try:
                if not prov.available():
                    continue
                if await prov.send(message):
                    any_ok = True
            except Exception as exc:
                self._ctx.log.warning("notifier provider %s failed: %s", name, exc)
        return any_ok

    async def send_test(
        self,
        title: str = "SimManager test notification",
        body: str = "Notifications are working.",
    ) -> dict[str, bool]:
        msg = NotificationMessage(
            title=title,
            body=body,
            severity=NotificationSeverity.INFO,
            category="test",
        )
        results: dict[str, bool] = {}
        for name, prov in self._provider_map().items():
            try:
                if not prov.available():
                    results[name] = False
                else:
                    results[name] = await prov.send(msg)
            except Exception as exc:
                self._ctx.log.warning("notifier test failed for %s: %s", name, exc)
                results[name] = False
        return results

    async def on_storage_event(self, payload: Any) -> None:
        if not isinstance(payload, EventRecord):
            return
        msg = format_event_notification(payload, self._cfg)
        if msg is not None:
            await self.notify(msg)

    async def close(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task


class NotifierRouterPlugin(Plugin):
    name = "notifier"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        storage_contract.SERVICE_KEY: storage_contract.CONTRACT_VERSION
    }
    config_schema = NotifierConfig
    description = "Routes alerts and cluster/job events to notifier providers."

    def apply(self, ctx: Context) -> NotificationRouter:
        assert isinstance(ctx.config, NotifierConfig)
        router = NotificationRouter(ctx, ctx.config)
        ctx.on(storage_contract.EVENT_APPENDED, router.on_storage_event)
        ctx.add_disposer(router.close, "stop notifier batch timer")
        return router
