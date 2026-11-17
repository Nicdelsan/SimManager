"""`rules` plugin: event-driven detection rules with hysteresis and adaptive stall."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import campaign as campaign_contract
from simmanager.contracts import ingest as ingest_contract
from simmanager.contracts import monitor as monitor_contract
from simmanager.contracts import rules as contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts import timeseries as ts_contract
from simmanager.contracts import transport as transport_contract
from simmanager.contracts.campaign import CampaignStore
from simmanager.contracts.rules import (
    AlertRaise,
    AlertRecord,
    EffectiveRule,
    RuleConfigRecord,
    RuleKind,
    RuleRunState,
    RuleScope,
    RuleSeverity,
    RuleSpec,
    RuleStore,
)
from simmanager.contracts.scheduler import JobState
from simmanager.contracts.storage import EventRecord, Storage
from simmanager.contracts.timeseries import SolverState, TimeSeriesStore
from simmanager.contracts.transport import ConnectionState, Transport
from simmanager.kernel import Context, Plugin

from .evaluator import (
    adaptive_stall_threshold_s,
    evaluate_points,
    match_series_for_rule,
    merge_effective_rules,
)


class MonitorRuleEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    selector: str
    min: float | None = None
    max: float | None = None
    consecutive: int = Field(default=3, ge=1)
    clear_consecutive: int = Field(default=1, ge=1)
    severity: RuleSeverity = RuleSeverity.WARNING
    enabled: bool = True


class RulesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_interval_s: float = Field(default=30.0, gt=0)
    nan_inf_enabled: bool = True
    solver_error_enabled: bool = True
    job_failed_enabled: bool = True

    courant_enabled: bool = True
    courant_threshold: float = Field(default=1.0, gt=0)
    courant_consecutive: int = Field(default=3, ge=1)
    courant_clear_consecutive: int = Field(default=2, ge=1)

    residual_enabled: bool = True
    residual_threshold: float = Field(default=1.0, gt=0)
    residual_consecutive: int = Field(default=5, ge=1)
    residual_clear_consecutive: int = Field(default=2, ge=1)
    residual_warmup_steps: int = Field(default=10, ge=0)
    residual_series_prefix: str = "residual."

    stall_enabled: bool = True
    stall_min_minutes: float = Field(default=30.0, gt=0)
    stall_multiplier: float = Field(default=3.0, gt=0)
    stall_max_intervals: int = Field(default=50, ge=5)

    monitors: list[MonitorRuleEntry] = Field(default_factory=list)


_STALL_RULE_KEY = "stall"
_STALL_KINDS = frozenset({RuleKind.STALL})


class RuleEngineService:
    """Implements `simmanager.contracts.rules.RuleEngine`."""

    def __init__(
        self,
        ctx: Context,
        storage: Storage,
        ts: TimeSeriesStore,
        store: RuleStore,
        cfg: RulesConfig,
    ) -> None:
        self._ctx = ctx
        self._storage = storage
        self._ts = ts
        self._store = store
        self._cfg = cfg
        self._run_locks: dict[int, asyncio.Lock] = {}

    def _run_lock(self, run_id: int) -> asyncio.Lock:
        lock = self._run_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[run_id] = lock
        return lock

    def _default_rules(self) -> list[EffectiveRule]:
        rules: list[EffectiveRule] = [
            EffectiveRule(
                rule_key="nan_inf",
                rule_kind=RuleKind.NAN_INF,
                enabled=self._cfg.nan_inf_enabled,
                severity=RuleSeverity.CRITICAL,
                params={},
                origin_scope=RuleScope.GLOBAL,
            ),
            EffectiveRule(
                rule_key="solver_error",
                rule_kind=RuleKind.SOLVER_ERROR,
                enabled=self._cfg.solver_error_enabled,
                severity=RuleSeverity.CRITICAL,
                params={},
                origin_scope=RuleScope.GLOBAL,
            ),
            EffectiveRule(
                rule_key="job_failed",
                rule_kind=RuleKind.JOB_FAILED,
                enabled=self._cfg.job_failed_enabled,
                severity=RuleSeverity.CRITICAL,
                params={},
                origin_scope=RuleScope.GLOBAL,
            ),
            EffectiveRule(
                rule_key="courant_high",
                rule_kind=RuleKind.COURANT_HIGH,
                enabled=self._cfg.courant_enabled,
                severity=RuleSeverity.WARNING,
                params={
                    "series": "courant.max",
                    "threshold": self._cfg.courant_threshold,
                    "consecutive": self._cfg.courant_consecutive,
                    "clear_consecutive": self._cfg.courant_clear_consecutive,
                },
                origin_scope=RuleScope.GLOBAL,
            ),
            EffectiveRule(
                rule_key="residual_high",
                rule_kind=RuleKind.RESIDUAL_HIGH,
                enabled=self._cfg.residual_enabled,
                severity=RuleSeverity.WARNING,
                params={
                    "series_prefix": self._cfg.residual_series_prefix,
                    "threshold": self._cfg.residual_threshold,
                    "consecutive": self._cfg.residual_consecutive,
                    "clear_consecutive": self._cfg.residual_clear_consecutive,
                    "warmup_steps": self._cfg.residual_warmup_steps,
                },
                origin_scope=RuleScope.GLOBAL,
            ),
            EffectiveRule(
                rule_key=_STALL_RULE_KEY,
                rule_kind=RuleKind.STALL,
                enabled=self._cfg.stall_enabled,
                severity=RuleSeverity.WARNING,
                params={
                    "min_minutes": self._cfg.stall_min_minutes,
                    "multiplier": self._cfg.stall_multiplier,
                    "max_intervals": self._cfg.stall_max_intervals,
                },
                origin_scope=RuleScope.GLOBAL,
            ),
        ]
        for m in self._cfg.monitors:
            rules.append(
                EffectiveRule(
                    rule_key=f"monitor:{m.key}",
                    rule_kind=RuleKind.MONITOR_RANGE,
                    enabled=m.enabled,
                    severity=m.severity,
                    params={
                        "selector": m.selector,
                        "min": m.min,
                        "max": m.max,
                        "consecutive": m.consecutive,
                        "clear_consecutive": m.clear_consecutive,
                    },
                    origin_scope=RuleScope.GLOBAL,
                )
            )
        return rules

    async def _resolve_campaign_case(self, run_id: int | None) -> tuple[int | None, int | None]:
        if run_id is None:
            return None, None
        camp_store: CampaignStore | None = self._ctx.services.get(campaign_contract.SERVICE_KEY)
        if camp_store is None:
            return None, None
        links = await camp_store.links_for_run(run_id)
        if not links:
            return None, None
        link = links[0]
        return link.campaign_id, link.case_id

    async def effective_rules(
        self,
        *,
        run_id: int | None = None,
        campaign_id: int | None = None,
        case_id: int | None = None,
    ) -> list[EffectiveRule]:
        if run_id is not None and campaign_id is None and case_id is None:
            campaign_id, case_id = await self._resolve_campaign_case(run_id)
        db_configs = await self._store.list_rule_configs()
        return merge_effective_rules(
            self._default_rules(), db_configs, campaign_id=campaign_id, case_id=case_id
        )

    async def set_rule(self, spec: RuleSpec) -> RuleConfigRecord:
        return await self._store.upsert_rule_config(spec)

    async def unset_rule(self, scope_type: RuleScope, scope_id: int, rule_key: str) -> bool:
        return await self._store.delete_rule_config(scope_type, scope_id, rule_key)

    async def list_alerts(
        self,
        *,
        open_only: bool = False,
        run_id: int | None = None,
        limit: int = 200,
    ) -> list[AlertRecord]:
        return await self._store.list_alerts(open_only=open_only, run_id=run_id, limit=limit)

    async def acknowledge(self, alert_id: int, *, by: str = "cli") -> AlertRecord | None:
        record = await self._store.acknowledge_alert(alert_id, by=by)
        if record is None:
            return None
        await self._storage.append_event(
            contract.EVENT_ALERT_ACKNOWLEDGED,
            {
                "alert_id": record.id,
                "rule_key": record.rule_key,
                "rule_kind": str(record.rule_kind),
                "by": by,
            },
            source=self._ctx.plugin_name,
            cluster_id=record.cluster_id,
            run_id=record.run_id,
        )
        return record

    # ------------------------------------------------------------------ alerts
    async def _raise(self, req: AlertRaise) -> AlertRecord:
        record, newly_opened = await self._store.raise_alert(req)
        if newly_opened:
            await self._storage.append_event(
                contract.EVENT_ALERT_RAISED,
                {
                    "alert_id": record.id,
                    "rule_key": record.rule_key,
                    "rule_kind": str(record.rule_kind),
                    "severity": str(record.severity),
                    "title": record.title,
                    "message": record.message,
                    "details": record.details,
                },
                source=self._ctx.plugin_name,
                cluster_id=record.cluster_id,
                run_id=record.run_id,
            )
        return record

    async def _clear(self, dedup_key: str) -> AlertRecord | None:
        cleared = await self._store.clear_alert(dedup_key)
        if cleared is not None:
            await self._storage.append_event(
                contract.EVENT_ALERT_CLEARED,
                {
                    "alert_id": cleared.id,
                    "rule_key": cleared.rule_key,
                    "rule_kind": str(cleared.rule_kind),
                    "title": cleared.title,
                },
                source=self._ctx.plugin_name,
                cluster_id=cleared.cluster_id,
                run_id=cleared.run_id,
            )
        return cleared

    async def _clear_run_stall(self, run_id: int) -> None:
        cleared = await self._store.clear_alerts_for_run(run_id, rule_kinds=_STALL_KINDS)
        for alert in cleared:
            await self._storage.append_event(
                contract.EVENT_ALERT_CLEARED,
                {
                    "alert_id": alert.id,
                    "rule_key": alert.rule_key,
                    "rule_kind": str(alert.rule_kind),
                    "title": alert.title,
                },
                source=self._ctx.plugin_name,
                cluster_id=alert.cluster_id,
                run_id=alert.run_id,
            )

    # ------------------------------------------------------------------ hooks
    async def on_storage_event(self, payload: Any) -> None:
        if not isinstance(payload, EventRecord):
            return
        if payload.kind != "job.state_changed" or payload.run_id is None:
            return
        run_id = payload.run_id
        to_state = str(payload.payload.get("to", ""))
        campaign_id, case_id = await self._resolve_campaign_case(run_id)
        rules = await self.effective_rules(run_id=run_id, campaign_id=campaign_id, case_id=case_id)

        if to_state == str(JobState.RUNNING):
            # Seed stall baseline when job starts running (if none observed yet)
            async with self._run_lock(run_id):
                st = await self._store.get_run_state(run_id, _STALL_RULE_KEY)
                if st is None:
                    await self._store.upsert_run_state(
                        RuleRunState(
                            run_id=run_id,
                            rule_key=_STALL_RULE_KEY,
                            last_observed_bytes=0,
                            last_growth_at=datetime.now(UTC),
                        )
                    )
            return

        if to_state in (
            str(JobState.COMPLETED),
            str(JobState.FAILED),
            str(JobState.CANCELLED),
            str(JobState.TIMEOUT),
            str(JobState.UNKNOWN),
        ):
            await self._clear_run_stall(run_id)

        if to_state in (str(JobState.FAILED), str(JobState.TIMEOUT)):
            rule = _find_rule(rules, RuleKind.JOB_FAILED)
            if rule is not None and rule.enabled:
                job_id = payload.payload.get("job_id", "?")
                reason = (
                    payload.payload.get("reason") or payload.payload.get("raw_state") or to_state
                )
                await self._raise(
                    AlertRaise(
                        dedup_key=f"run:{run_id}:{rule.rule_key}",
                        rule_key=rule.rule_key,
                        rule_kind=RuleKind.JOB_FAILED,
                        severity=rule.severity,
                        title=f"Job {job_id} {to_state}",
                        message=f"Job {job_id} ended with state {to_state} ({reason})",
                        cluster_id=payload.cluster_id,
                        run_id=run_id,
                        campaign_id=campaign_id,
                        case_id=case_id,
                        details=dict(payload.payload),
                    )
                )

    async def on_nan_detected(self, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            return
        run_id = payload.get("run_id")
        if not isinstance(run_id, int):
            return
        campaign_id, case_id = await self._resolve_campaign_case(run_id)
        rules = await self.effective_rules(run_id=run_id, campaign_id=campaign_id, case_id=case_id)
        rule = _find_rule(rules, RuleKind.NAN_INF)
        if rule is None or not rule.enabled:
            return
        col = payload.get("column", "?")
        fpath = payload.get("file_path", "")
        await self._raise(
            AlertRaise(
                dedup_key=f"run:{run_id}:{rule.rule_key}",
                rule_key=rule.rule_key,
                rule_kind=RuleKind.NAN_INF,
                severity=rule.severity,
                title=f"NaN/Inf detected in run {run_id}",
                message=f"Non-finite value in {col} ({fpath})",
                cluster_id=payload.get("cluster_id"),
                run_id=run_id,
                campaign_id=campaign_id,
                case_id=case_id,
                details=dict(payload),
            )
        )

    async def on_solver_error(self, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            return
        run_id = payload.get("run_id")
        if not isinstance(run_id, int):
            return
        await self._raise_solver_error(
            run_id,
            cluster_id=payload.get("cluster_id"),
            message=str(payload.get("message") or "Solver reported fatal error"),
            details=dict(payload),
        )

    async def on_solver_state_changed(self, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            return
        run_id = payload.get("run_id")
        if not isinstance(run_id, int):
            return
        to_state = str(payload.get("to", ""))
        if to_state in (
            str(SolverState.COMPLETED),
            str(SolverState.STOPPED),
            str(SolverState.FAILED),
        ):
            await self._clear_run_stall(run_id)
        if to_state == str(SolverState.FAILED):
            await self._raise_solver_error(
                run_id,
                cluster_id=payload.get("cluster_id"),
                message=str(payload.get("error_message") or "Solver transitioned to failed"),
                details=dict(payload),
            )

    async def _raise_solver_error(
        self,
        run_id: int,
        *,
        cluster_id: str | None,
        message: str,
        details: dict[str, Any],
    ) -> None:
        campaign_id, case_id = await self._resolve_campaign_case(run_id)
        rules = await self.effective_rules(run_id=run_id, campaign_id=campaign_id, case_id=case_id)
        rule = _find_rule(rules, RuleKind.SOLVER_ERROR)
        if rule is None or not rule.enabled:
            return
        await self._raise(
            AlertRaise(
                dedup_key=f"run:{run_id}:{rule.rule_key}",
                rule_key=rule.rule_key,
                rule_kind=RuleKind.SOLVER_ERROR,
                severity=rule.severity,
                title=f"Solver error on run {run_id}",
                message=message,
                cluster_id=cluster_id,
                run_id=run_id,
                campaign_id=campaign_id,
                case_id=case_id,
                details=details,
            )
        )

    async def on_file_truncated(self, payload: Any) -> None:
        """Reset residual warmup counters when a file is truncated / restarted in place."""
        if not isinstance(payload, Mapping):
            return
        run_id = payload.get("run_id")
        file_path = payload.get("file_path")
        if not isinstance(run_id, int):
            return
        async with self._run_lock(run_id):
            rules = await self.effective_rules(run_id=run_id)
            res_rule = _find_rule(rules, RuleKind.RESIDUAL_HIGH)
            if res_rule is None:
                return
            warmup = max(0, int(res_rule.params.get("warmup_steps", 0)))
            for st in await self._store.list_run_states(run_id):
                if st.rule_key != res_rule.rule_key:
                    continue
                if isinstance(file_path, str) and not st.series_tag.startswith(f"{file_path}:"):
                    continue
                await self._store.upsert_run_state(
                    RuleRunState(
                        run_id=run_id,
                        rule_key=st.rule_key,
                        series_tag=st.series_tag,
                        last_x=None,
                        consecutive_hits=0,
                        consecutive_clear=0,
                        warmup_remaining=warmup,
                    )
                )

    async def on_series_updated(self, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            return
        run_id = payload.get("run_id")
        if not isinstance(run_id, int):
            return
        cluster_id = payload.get("cluster_id")
        async with self._run_lock(run_id):
            await self._record_growth(run_id)
            await self._evaluate_series_rules(run_id, cluster_id=cluster_id)

    async def _record_growth(self, run_id: int) -> None:
        cursors = await self._ts.list_cursors(run_id)
        if not cursors:
            return
        # Never attribute growth intervals while backfill is active
        if any(c.needs_backfill for c in cursors):
            return
        total_bytes = sum(c.file_size for c in cursors)
        now = datetime.now(UTC)
        state = await self._store.get_run_state(run_id, _STALL_RULE_KEY)
        if state is None or state.last_growth_at is None or state.last_observed_bytes is None:
            await self._store.upsert_run_state(
                RuleRunState(
                    run_id=run_id,
                    rule_key=_STALL_RULE_KEY,
                    last_observed_bytes=total_bytes,
                    last_growth_at=now,
                    growth_intervals_s=state.growth_intervals_s if state else (),
                )
            )
            return

        if total_bytes > state.last_observed_bytes:
            delta_s = (now - state.last_growth_at).total_seconds()
            intervals = list(state.growth_intervals_s)
            if delta_s >= 1.0:
                intervals.append(delta_s)
                if len(intervals) > self._cfg.stall_max_intervals:
                    intervals = intervals[-self._cfg.stall_max_intervals :]
            await self._store.upsert_run_state(
                RuleRunState(
                    run_id=run_id,
                    rule_key=_STALL_RULE_KEY,
                    last_observed_bytes=total_bytes,
                    last_growth_at=now,
                    growth_intervals_s=tuple(intervals),
                )
            )
            await self._clear_run_stall(run_id)

    async def _evaluate_series_rules(self, run_id: int, *, cluster_id: str | None = None) -> None:
        campaign_id, case_id = await self._resolve_campaign_case(run_id)
        rules = await self.effective_rules(run_id=run_id, campaign_id=campaign_id, case_id=case_id)
        all_series = [
            s
            for s in await self._ts.list_series(run_id)
            if s.file_path != ingest_contract.PROGRESS_SERIES.file_path
        ]
        if not all_series:
            return

        for rule in rules:
            if not rule.enabled or rule.rule_kind not in (
                RuleKind.COURANT_HIGH,
                RuleKind.RESIDUAL_HIGH,
                RuleKind.MONITOR_RANGE,
            ):
                continue
            matched = match_series_for_rule(rule, all_series)
            for s in matched:
                tag = f"{s.file_path}:{s.name}:{s.qualifier}"
                st = await self._store.get_run_state(run_id, rule.rule_key, tag)
                if st is None:
                    warmup = (
                        max(0, int(rule.params.get("warmup_steps", 0)))
                        if rule.rule_kind is RuleKind.RESIDUAL_HIGH
                        else 0
                    )
                    st = RuleRunState(
                        run_id=run_id,
                        rule_key=rule.rule_key,
                        series_tag=tag,
                        warmup_remaining=warmup,
                    )
                if st.last_x is not None and s.last_x is not None and s.last_x == st.last_x:
                    continue
                if st.last_x is not None and s.last_x is not None and s.last_x < st.last_x:
                    warmup = (
                        max(0, int(rule.params.get("warmup_steps", 0)))
                        if rule.rule_kind is RuleKind.RESIDUAL_HIGH
                        else 0
                    )
                    st = RuleRunState(
                        run_id=run_id,
                        rule_key=rule.rule_key,
                        series_tag=tag,
                        last_x=None,
                        warmup_remaining=warmup,
                    )

                pts = await self._ts.query_series(s.id, x_min=st.last_x, max_points=1_000_000)
                if not pts.x:
                    continue
                res = evaluate_points(
                    rule,
                    pts.x,
                    pts.y,
                    last_x=st.last_x,
                    consecutive_hits=st.consecutive_hits,
                    consecutive_clear=st.consecutive_clear,
                    warmup_remaining=st.warmup_remaining,
                )
                await self._store.upsert_run_state(
                    RuleRunState(
                        run_id=run_id,
                        rule_key=rule.rule_key,
                        series_tag=tag,
                        last_x=res.last_x,
                        consecutive_hits=res.consecutive_hits,
                        consecutive_clear=res.consecutive_clear,
                        warmup_remaining=res.warmup_remaining,
                    )
                )
                dedup = f"run:{run_id}:{rule.rule_key}:{tag}"
                if res.should_raise:
                    await self._raise(
                        AlertRaise(
                            dedup_key=dedup,
                            rule_key=rule.rule_key,
                            rule_kind=rule.rule_kind,
                            severity=rule.severity,
                            title=f"{rule.rule_key} triggered on {s.name}",
                            message=(
                                f"{s.name} ({s.file_path}) = {res.last_trigger_value} "
                                f"at x={res.last_trigger_x}"
                            ),
                            cluster_id=cluster_id,
                            run_id=run_id,
                            campaign_id=campaign_id,
                            case_id=case_id,
                            details={
                                "series": s.name,
                                "qualifier": s.qualifier,
                                "file_path": s.file_path,
                                "value": res.last_trigger_value,
                                "x": res.last_trigger_x,
                            },
                        )
                    )
                elif res.should_clear:
                    await self._clear(dedup)

    async def on_monitor_polled(self, payload: Any) -> None:
        await self.evaluate_stall_now()

    async def evaluate_stall_now(self, *, now: datetime | None = None) -> list[AlertRecord]:
        now_utc = now or datetime.now(UTC)
        transport: Transport | None = self._ctx.services.get(transport_contract.SERVICE_KEY)
        raised: list[AlertRecord] = []

        running_runs: dict[int, str | None] = {}
        for job in await self._storage.jobs(active=True, limit=500):
            if job.run_id is not None and job.info.state is JobState.RUNNING:
                running_runs[job.run_id] = job.cluster_id

        # Also check active manual runs whose solver state is RUNNING
        for run in await self._storage.runs(limit=200):
            if run.origin != "manual" or run.id in running_runs:
                continue
            sstate = await self._ts.get_run_solver_state(run.id)
            if sstate is not None and sstate.solver_state is SolverState.RUNNING:
                running_runs[run.id] = run.cluster_id

        for run_id, cluster_id in running_runs.items():
            if (
                transport is not None
                and cluster_id is not None
                and cluster_id in transport.clusters()
                and transport.state(cluster_id) is not ConnectionState.CONNECTED
            ):
                continue

            cursors = await self._ts.list_cursors(run_id)
            if any(c.needs_backfill for c in cursors):
                continue

            campaign_id, case_id = await self._resolve_campaign_case(run_id)
            rules = await self.effective_rules(
                run_id=run_id, campaign_id=campaign_id, case_id=case_id
            )
            rule = _find_rule(rules, RuleKind.STALL)
            if rule is None or not rule.enabled:
                continue

            st = await self._store.get_run_state(run_id, _STALL_RULE_KEY)
            if st is None or st.last_growth_at is None:
                continue

            threshold_s = adaptive_stall_threshold_s(
                st.growth_intervals_s,
                min_minutes=float(rule.params.get("min_minutes", self._cfg.stall_min_minutes)),
                multiplier=float(rule.params.get("multiplier", self._cfg.stall_multiplier)),
            )
            idle_s = (now_utc - st.last_growth_at).total_seconds()
            if idle_s >= threshold_s:
                alert = await self._raise(
                    AlertRaise(
                        dedup_key=f"run:{run_id}:{rule.rule_key}",
                        rule_key=rule.rule_key,
                        rule_kind=RuleKind.STALL,
                        severity=rule.severity,
                        title=f"Run {run_id} stalled",
                        message=(
                            f"No file growth observed for {idle_s / 60:.1f} min "
                            f"(threshold {threshold_s / 60:.1f} min)"
                        ),
                        cluster_id=cluster_id,
                        run_id=run_id,
                        campaign_id=campaign_id,
                        case_id=case_id,
                        details={
                            "idle_s": round(idle_s, 1),
                            "threshold_s": round(threshold_s, 1),
                            "intervals_count": len(st.growth_intervals_s),
                        },
                    )
                )
                raised.append(alert)

        return raised

    async def stall_loop(self) -> None:
        try:
            while not self._ctx.closed:
                await asyncio.sleep(self._cfg.check_interval_s)
                try:
                    await self.evaluate_stall_now()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._ctx.log.exception("stall check failed")
        except asyncio.CancelledError:
            return


def _find_rule(rules: list[EffectiveRule], kind: RuleKind) -> EffectiveRule | None:
    return next((r for r in rules if r.rule_kind is kind), None)


class RulesPlugin(Plugin):
    name = "rules"
    provides = contract.MANAGER_SERVICE_KEY
    contract_version = contract.MANAGER_CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        storage_contract.SERVICE_KEY: storage_contract.CONTRACT_VERSION,
        ts_contract.SERVICE_KEY: ts_contract.CONTRACT_VERSION,
        contract.SERVICE_KEY: contract.CONTRACT_VERSION,
    }
    config_schema = RulesConfig
    description = "Event-driven detection rules with hysteresis, warmup, and adaptive stall."

    def apply(self, ctx: Context) -> RuleEngineService:
        assert isinstance(ctx.config, RulesConfig)
        storage: Storage = ctx.services.require(storage_contract.SERVICE_KEY)
        ts: TimeSeriesStore = ctx.services.require(ts_contract.SERVICE_KEY)
        store: RuleStore = ctx.services.require(contract.SERVICE_KEY)
        engine = RuleEngineService(ctx, storage, ts, store, ctx.config)
        ctx.on(storage_contract.EVENT_APPENDED, engine.on_storage_event)
        ctx.on("solver.nan_detected", engine.on_nan_detected)
        ctx.on("solver.error", engine.on_solver_error)
        ctx.on("run.solver_state_changed", engine.on_solver_state_changed)
        ctx.on("file.truncated", engine.on_file_truncated)
        ctx.on("series.updated", engine.on_series_updated)
        ctx.on(monitor_contract.POLLED_EVENT, engine.on_monitor_polled)
        ctx.task(engine.stall_loop(), name="rules-stall-loop")
        return engine
