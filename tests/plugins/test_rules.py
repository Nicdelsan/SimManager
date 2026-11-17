from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from simmanager.contracts.campaign import CampaignSpec, CaseSpec, MatchKind
from simmanager.contracts.clusters import ClusterProfile
from simmanager.contracts.rules import (
    AlertStatus,
    RuleEngine,
    RuleKind,
    RuleScope,
    RuleSeverity,
    RuleSpec,
)
from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.contracts.storage import Storage
from simmanager.contracts.timeseries import (
    FileCursor,
    FileStatus,
    SeriesKey,
    TimeSeriesStore,
)
from simmanager.contracts.transport import ConnectionState
from simmanager.kernel import Kernel, Plugin
from simmanager.plugins.monitor import MonitorPlugin
from simmanager.plugins.rules import RulesPlugin, adaptive_stall_threshold_s
from simmanager.plugins.storage_sqlite import (
    SqliteCampaignPlugin,
    SqliteRulePlugin,
    SqliteStoragePlugin,
    SqliteTimeSeriesPlugin,
)
from simmanager.readers import LocalRemoteSession


async def _wait_for(predicate: object, *, limit_s: float = 3.0) -> None:
    async with asyncio.timeout(limit_s):
        while True:
            if callable(predicate) and await predicate():  # type: ignore[misc]
                return
            await asyncio.sleep(0.02)


class _ToggleTransport:
    def __init__(self, profiles: Mapping[str, ClusterProfile], session: LocalRemoteSession) -> None:
        self._profiles = dict(profiles)
        self._session = session
        self.conn_state = ConnectionState.CONNECTED

    def clusters(self) -> Mapping[str, ClusterProfile]:
        return self._profiles

    async def session(self, cluster_id: str) -> LocalRemoteSession:
        return self._session

    def state(self, cluster_id: str) -> ConnectionState:
        return self.conn_state

    async def reset(self, cluster_id: str) -> None:
        return None


def _make_transport_plugin(t: _ToggleTransport) -> type[Plugin]:
    class _P(Plugin):
        name = "fake-transport"
        provides = "transport"
        contract_version = 1

        def apply(self, ctx: object) -> _ToggleTransport:
            return t

    return _P


class _FakeScheduler:
    def __init__(self) -> None:
        self.active: list[JobInfo] = []
        self.finals: dict[str, JobInfo] = {}

    async def list_active(self, session: object, user: str, tz: object) -> list[JobInfo]:
        return list(self.active)

    async def final_states(
        self, session: object, job_ids: Sequence[str], tz: object
    ) -> dict[str, JobInfo]:
        return {jid: self.finals[jid] for jid in job_ids if jid in self.finals}

    def cancel_argv(self, job_id: str) -> list[str]:
        return ["scancel", job_id]


def _make_scheduler_plugin(s: _FakeScheduler) -> type[Plugin]:
    class _P(Plugin):
        name = "fake-scheduler"
        provides = "scheduler.slurm"
        contract_version = 1

        def apply(self, ctx: object) -> _FakeScheduler:
            return s

    return _P


async def test_multilevel_rules_and_hysteresis_and_restart(tmp_path: Path) -> None:
    db_path = str(tmp_path / "sim.db")
    cfg = {
        "storage-sqlite": {"path": db_path},
        "rules": {
            "courant_threshold": 1.0,
            "courant_consecutive": 2,
            "courant_clear_consecutive": 2,
            "check_interval_s": 60.0,
        },
    }
    kernel = Kernel(cfg)
    await kernel.add(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        SqliteCampaignPlugin,
        SqliteRulePlugin,
        RulesPlugin,
    )
    storage: Storage = kernel.services.require("storage")
    ts: TimeSeriesStore = kernel.services.require("timeseries")
    camp_store = kernel.services.require("campaign")
    engine: RuleEngine = kernel.services.require("rules.manager")

    camp = await camp_store.upsert_campaign(CampaignSpec(project_name="p", name="c1"))
    case1, _, _ = await camp_store.upsert_case(camp.id, CaseSpec("001", work_dir="/work/u/001"))
    run = await storage.create_run(work_dir="/work/u/001", cluster_id="c1")
    await camp_store.link_run(camp.id, case1.id, run.id, match_kind=MatchKind.MANUAL)

    # Campaign override lowers Courant threshold to 0.8; case adds monitor_range
    await engine.set_rule(
        RuleSpec(
            scope_type=RuleScope.CAMPAIGN,
            scope_id=camp.id,
            rule_key="courant_high",
            rule_kind=RuleKind.COURANT_HIGH,
            severity=RuleSeverity.CRITICAL,
            params={"threshold": 0.8},
        )
    )
    await engine.set_rule(
        RuleSpec(
            scope_type=RuleScope.CASE,
            scope_id=case1.id,
            rule_key="monitor:max_temp",
            rule_kind=RuleKind.MONITOR_RANGE,
            severity=RuleSeverity.WARNING,
            params={"selector": "thermo.out:Max_Temp:bound_1", "max": 2500.0, "consecutive": 2},
        )
    )
    eff = {r.rule_key: r for r in await engine.effective_rules(run_id=run.id)}
    assert eff["courant_high"].params["threshold"] == 0.8
    assert eff["courant_high"].severity is RuleSeverity.CRITICAL
    assert eff["monitor:max_temp"].origin_scope is RuleScope.CASE

    # Emit 2 consecutive points above 0.8 -> raises once
    co_key = SeriesKey("log.out", "courant.max", "", "", "Time", "s")
    t_key = SeriesKey("thermo.out", "Max_Temp", "bound_1", "K", "Time", "s")
    await ts.append_samples(
        run.id,
        {
            co_key: ([0.1, 0.2], [0.9, 0.95]),
            t_key: ([0.1, 0.2], [2600.0, 2700.0]),
        },
    )
    await kernel.bus.emit("series.updated", {"run_id": run.id, "cluster_id": "c1"})

    open_alerts = await engine.list_alerts(open_only=True, run_id=run.id)
    assert len(open_alerts) == 2
    co_alert = next(a for a in open_alerts if a.rule_kind is RuleKind.COURANT_HIGH)
    assert co_alert.severity is RuleSeverity.CRITICAL

    # Ack keeps alert open and another high batch does not duplicate
    acked = await engine.acknowledge(co_alert.id, by="user1")
    assert acked is not None and acked.acknowledged_by == "user1"
    await ts.append_samples(run.id, {co_key: ([0.3], [1.2])})
    await kernel.bus.emit("series.updated", {"run_id": run.id, "cluster_id": "c1"})
    assert len(await engine.list_alerts(open_only=True, run_id=run.id)) == 2
    assert len(await storage.events(kinds=["alert.raised"], run_id=run.id)) == 2

    # Simulate kernel restart with persistent SQLite db: still no duplicate alert.raised!
    await kernel.stop()
    kernel2 = Kernel(cfg)
    await kernel2.add(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        SqliteCampaignPlugin,
        SqliteRulePlugin,
        RulesPlugin,
    )
    storage2: Storage = kernel2.services.require("storage")
    ts2: TimeSeriesStore = kernel2.services.require("timeseries")
    engine2: RuleEngine = kernel2.services.require("rules.manager")

    await ts2.append_samples(run.id, {co_key: ([0.4], [1.1])})
    await kernel2.bus.emit("series.updated", {"run_id": run.id, "cluster_id": "c1"})
    assert len(await storage2.events(kinds=["alert.raised"], run_id=run.id)) == 2

    # Condition clears after 2 normal points (clear_consecutive=2)
    await ts2.append_samples(run.id, {co_key: ([0.5, 0.6], [0.4, 0.3])})
    await kernel2.bus.emit("series.updated", {"run_id": run.id, "cluster_id": "c1"})
    all_co = [
        a for a in await engine2.list_alerts(run_id=run.id) if a.rule_kind is RuleKind.COURANT_HIGH
    ]
    assert len(all_co) == 1
    assert all_co[0].status is AlertStatus.CLEARED
    assert len(await storage2.events(kinds=["alert.cleared"], run_id=run.id)) == 1

    # Unset rule removes override
    assert await engine2.unset_rule(RuleScope.CASE, case1.id, "monitor:max_temp") is True
    eff2 = {r.rule_key: r for r in await engine2.effective_rules(run_id=run.id)}
    assert "monitor:max_temp" not in eff2

    await kernel2.stop()


async def test_residual_warmup_and_restart_and_nan(tmp_path: Path) -> None:
    kernel = Kernel(
        {
            "storage-sqlite": {"path": str(tmp_path / "sim.db")},
            "rules": {
                "residual_threshold": 0.5,
                "residual_consecutive": 2,
                "residual_warmup_steps": 3,
                "check_interval_s": 60.0,
            },
        }
    )
    await kernel.add(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        SqliteRulePlugin,
        RulesPlugin,
    )
    storage: Storage = kernel.services.require("storage")
    ts: TimeSeriesStore = kernel.services.require("timeseries")
    engine: RuleEngine = kernel.services.require("rules.manager")
    run = await storage.create_run(work_dir="/work/u/res", cluster_id="c1")

    res_key = SeriesKey("log.out", "residual.p", "", "", "Time", "s")
    # First 3 steps (0.1, 0.2, 0.3) are high but inside warmup -> ignored!
    await ts.append_samples(run.id, {res_key: ([0.1, 0.2, 0.3, 0.4], [0.9, 0.9, 0.9, 0.1])})
    await kernel.bus.emit("series.updated", {"run_id": run.id, "cluster_id": "c1"})
    assert await engine.list_alerts(open_only=True, run_id=run.id) == []

    # After warmup: 2 consecutive high points -> raises!
    await ts.append_samples(run.id, {res_key: ([0.5, 0.6], [0.8, 0.7])})
    await kernel.bus.emit("series.updated", {"run_id": run.id, "cluster_id": "c1"})
    alerts = await engine.list_alerts(open_only=True, run_id=run.id)
    assert len(alerts) == 1 and alerts[0].rule_kind is RuleKind.RESIDUAL_HIGH

    # Clear residual alert, then trigger restart (file.truncated) -> another 3 warmup steps ignored!
    await ts.append_samples(run.id, {res_key: ([0.7, 0.8], [0.1, 0.1])})
    await kernel.bus.emit("series.updated", {"run_id": run.id, "cluster_id": "c1"})
    assert await engine.list_alerts(open_only=True, run_id=run.id) == []

    await ts.reset_file_data(run.id, "log.out")
    await kernel.bus.emit("file.truncated", {"run_id": run.id, "file_path": "log.out"})
    await ts.append_samples(run.id, {res_key: ([0.1, 0.2, 0.3, 0.4], [0.95, 0.95, 0.95, 0.05])})
    await kernel.bus.emit("series.updated", {"run_id": run.id, "cluster_id": "c1"})
    assert await engine.list_alerts(open_only=True, run_id=run.id) == []

    # NaN event turns into critical alert
    await kernel.bus.emit(
        "solver.nan_detected",
        {"run_id": run.id, "cluster_id": "c1", "file_path": "thermo.out", "column": "Temp"},
    )
    nan_alerts = await engine.list_alerts(open_only=True, run_id=run.id)
    assert len(nan_alerts) == 1 and nan_alerts[0].rule_kind is RuleKind.NAN_INF

    await kernel.stop()


async def test_adaptive_stall_respects_backfill_and_disconnection(tmp_path: Path) -> None:
    assert adaptive_stall_threshold_s([], min_minutes=30.0, multiplier=3.0) == 1800.0
    assert (
        adaptive_stall_threshold_s([60.0, 120.0, 180.0], min_minutes=30.0, multiplier=3.0) == 1800.0
    )
    # Median = 900s * 3 = 2700s (45 min) > 30 min floor
    assert (
        adaptive_stall_threshold_s([900.0, 900.0, 1200.0], min_minutes=30.0, multiplier=3.0)
        == 2700.0
    )

    case_dir = tmp_path / "case_stall"
    case_dir.mkdir()
    session = LocalRemoteSession(case_dir, cluster_id="c1", virtual_root="/work/u/stall")
    transport = _ToggleTransport(
        {"c1": ClusterProfile(host="cluster.example.org", user="user1")}, session
    )
    sched = _FakeScheduler()

    kernel = Kernel(
        {
            "storage-sqlite": {"path": str(tmp_path / "sim.db")},
            "monitor": {"loop": False},
            "rules": {"stall_min_minutes": 30.0, "stall_multiplier": 3.0, "check_interval_s": 60.0},
        }
    )
    await kernel.add(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        SqliteRulePlugin,
        RulesPlugin,
        MonitorPlugin,
        _make_transport_plugin(transport),
        _make_scheduler_plugin(sched),
    )
    storage: Storage = kernel.services.require("storage")
    ts: TimeSeriesStore = kernel.services.require("timeseries")
    engine: RuleEngine = kernel.services.require("rules.manager")
    monitor = kernel.services.require("monitor")

    sched.active = [
        JobInfo(
            job_id="701",
            name="stall_job",
            state=JobState.RUNNING,
            raw_state="RUNNING",
            work_dir="/work/u/stall",
        )
    ]
    await monitor.poll("c1")
    job = (await storage.jobs(cluster_id="c1"))[0]
    assert job.run_id is not None
    run_id = job.run_id

    # Cursor with backfill pending -> stall must NOT trigger even far in the future!
    await ts.upsert_cursor(
        FileCursor(
            run_id=run_id,
            file_path="logout.txt",
            parser_kind="converge_stdout",
            file_size=1024,
            forward_offset=1024,
            backfill_start=100,
            backfill_end=500,
            status=FileStatus.BACKFILLING,
        )
    )
    # Seed initial growth state
    store = kernel.services.require("rules")
    t0 = datetime.now(UTC) - timedelta(minutes=120)
    from simmanager.contracts.rules import RuleRunState

    await store.upsert_run_state(
        RuleRunState(
            run_id=run_id,
            rule_key="stall",
            last_observed_bytes=1024,
            last_growth_at=t0,
            growth_intervals_s=(900.0, 900.0, 900.0),  # 45 min adaptive threshold
        )
    )
    assert await engine.evaluate_stall_now(now=t0 + timedelta(minutes=60)) == []

    # Finish backfill, disconnect cluster -> stall must NOT trigger!
    await ts.upsert_cursor(
        FileCursor(
            run_id=run_id,
            file_path="logout.txt",
            parser_kind="converge_stdout",
            file_size=1024,
            forward_offset=1024,
            backfill_start=500,
            backfill_end=500,
            status=FileStatus.ACTIVE,
        )
    )
    transport.conn_state = ConnectionState.BACKOFF
    assert await engine.evaluate_stall_now(now=t0 + timedelta(minutes=60)) == []

    # Reconnect: at +35 min (< 45 min adaptive threshold) still not stalled; at +50 min -> stalled!
    transport.conn_state = ConnectionState.CONNECTED
    assert await engine.evaluate_stall_now(now=t0 + timedelta(minutes=35)) == []
    raised = await engine.evaluate_stall_now(now=t0 + timedelta(minutes=50))
    assert len(raised) == 1 and raised[0].rule_kind is RuleKind.STALL

    # New bytes arrive -> series.updated clears stall
    await ts.upsert_cursor(
        FileCursor(
            run_id=run_id,
            file_path="logout.txt",
            parser_kind="converge_stdout",
            file_size=4096,
            forward_offset=4096,
            backfill_start=500,
            backfill_end=500,
            status=FileStatus.ACTIVE,
        )
    )
    await kernel.bus.emit("series.updated", {"run_id": run_id, "cluster_id": "c1"})
    assert await engine.list_alerts(open_only=True, run_id=run_id) == []

    await kernel.stop()


async def test_real_monitor_job_failed_vs_completed(tmp_path: Path) -> None:
    case_dir = tmp_path / "case_mon_rules"
    case_dir.mkdir()
    session = LocalRemoteSession(case_dir, cluster_id="c1", virtual_root="/work/u/mon")
    transport = _ToggleTransport(
        {"c1": ClusterProfile(host="cluster.example.org", user="user1")}, session
    )
    sched = _FakeScheduler()

    kernel = Kernel(
        {
            "storage-sqlite": {"path": str(tmp_path / "sim.db")},
            "monitor": {"loop": False},
            "rules": {"check_interval_s": 60.0},
        }
    )
    await kernel.add(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        SqliteRulePlugin,
        RulesPlugin,
        MonitorPlugin,
        _make_transport_plugin(transport),
        _make_scheduler_plugin(sched),
    )
    engine: RuleEngine = kernel.services.require("rules.manager")
    monitor = kernel.services.require("monitor")

    # Job 801 fails, Job 802 completes
    sched.active = [
        JobInfo("801", "fail_job", JobState.RUNNING, "RUNNING", work_dir="/work/u/mon"),
        JobInfo("802", "ok_job", JobState.RUNNING, "RUNNING", work_dir="/work/u/mon"),
    ]
    await monitor.poll("c1")

    sched.active = []
    sched.finals = {
        "801": JobInfo(
            "801", "fail_job", JobState.FAILED, "FAILED", work_dir="/work/u/mon", exit_code="1:0"
        ),
        "802": JobInfo(
            "802",
            "ok_job",
            JobState.COMPLETED,
            "COMPLETED",
            work_dir="/work/u/mon",
            exit_code="0:0",
        ),
    }
    await monitor.poll("c1")

    async def _one_failed_alert() -> bool:
        return len(await engine.list_alerts(open_only=True)) == 1

    await _wait_for(_one_failed_alert)
    alerts = await engine.list_alerts(open_only=True)
    assert len(alerts) == 1
    assert alerts[0].rule_kind is RuleKind.JOB_FAILED
    assert "801" in alerts[0].title

    await kernel.stop()
