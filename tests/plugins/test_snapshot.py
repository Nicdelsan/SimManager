from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from simmanager.contracts.campaign import CampaignSpec, CaseSpec, MatchKind
from simmanager.contracts.clusters import ClusterProfile
from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.contracts.snapshot import (
    ContentStatus,
    DiffChangeKind,
    SnapshotService,
    SnapshotTrigger,
)
from simmanager.contracts.storage import Storage
from simmanager.contracts.transport import ConnectionState
from simmanager.kernel import Kernel, Plugin
from simmanager.plugins.monitor import MonitorPlugin
from simmanager.plugins.snapshot import (
    SnapshotPlugin,
    diff_openfoam_semantic,
    diff_yaml_semantic,
)
from simmanager.plugins.storage_sqlite import (
    SqliteCampaignPlugin,
    SqliteSnapshotPlugin,
    SqliteStorage,
    SqliteStoragePlugin,
)
from simmanager.readers import LocalRemoteSession

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class _CountingLocalSession(LocalRemoteSession):
    def __init__(self, root: Path, *, cluster_id: str = "c1", virtual_root: str) -> None:
        super().__init__(root, cluster_id=cluster_id, virtual_root=virtual_root)
        self.read_calls: dict[str, int] = {}

    async def read_range(self, path: str, offset: int, max_bytes: int) -> bytes:
        self.read_calls[path] = self.read_calls.get(path, 0) + 1
        return await super().read_range(path, offset, max_bytes)


class _FakeTransport:
    def __init__(self, profiles: Mapping[str, ClusterProfile], session: LocalRemoteSession) -> None:
        self._profiles = dict(profiles)
        self._session = session

    def clusters(self) -> Mapping[str, ClusterProfile]:
        return self._profiles

    async def session(self, cluster_id: str) -> LocalRemoteSession:
        return self._session

    def state(self, cluster_id: str) -> ConnectionState:
        return ConnectionState.CONNECTED

    async def reset(self, cluster_id: str) -> None:
        return None


def _make_transport_plugin(transport: _FakeTransport) -> type[Plugin]:
    class _FakeTransportPlugin(Plugin):
        name = "fake-transport"
        provides = "transport"
        contract_version = 1

        def apply(self, ctx: object) -> _FakeTransport:
            return transport

    return _FakeTransportPlugin


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


def _make_scheduler_plugin(sched: _FakeScheduler) -> type[Plugin]:
    class _FakeSchedulerPlugin(Plugin):
        name = "fake-scheduler"
        provides = "scheduler.slurm"
        contract_version = 1

        def apply(self, ctx: object) -> _FakeScheduler:
            return sched

    return _FakeSchedulerPlugin


async def _wait_for(predicate: object, *, limit_s: float = 3.0) -> None:
    async with asyncio.timeout(limit_s):
        while True:
            if callable(predicate) and await predicate():  # type: ignore[misc]
                return
            await asyncio.sleep(0.02)


def test_semantic_diff_yaml_and_openfoam_pure() -> None:
    v1 = (FIXTURES / "converge" / "solver_v1.in").read_text(encoding="utf-8")
    v2 = (FIXTURES / "converge" / "solver_v2.in").read_text(encoding="utf-8")
    changes = diff_yaml_semantic(v1, v2)
    by_path = {c.path: c for c in changes}
    assert by_path["max_cfl"].old_value == "0.8"
    assert by_path["max_cfl"].new_value == "0.5"
    assert by_path["relaxation.pressure"].old_value == "0.3"
    assert by_path["relaxation.pressure"].new_value == "0.25"
    assert by_path["new_limiter"].old_value is None
    assert by_path["new_limiter"].new_value == "true"

    of1 = (FIXTURES / "openfoam" / "system" / "controlDict").read_text(encoding="utf-8")
    of2 = of1.replace("5.0e-4", "1.0e-3").replace("5e-11", "1e-10")
    of_changes = {c.path: c for c in diff_openfoam_semantic(of1, of2)}
    assert of_changes["endTime"].old_value == "5.0e-4"
    assert of_changes["endTime"].new_value == "1.0e-3"
    assert of_changes["deltaT"].old_value == "5e-11"
    assert of_changes["deltaT"].new_value == "1e-10"


async def test_oversize_hash_background_and_reuse_and_blob_dedup(tmp_path: Path) -> None:
    case_dir = tmp_path / "case_a"
    case_dir.mkdir()
    shutil.copy(FIXTURES / "converge" / "inputs.in", case_dir / "inputs.in")
    shutil.copy(FIXTURES / "converge" / "solver_v1.in", case_dir / "solver.in")
    surface = case_dir / "surface.dat"
    surface.write_bytes(b"SURFDATA" * 4000)  # 32 KB, > max_file_bytes=4096

    db_path = str(tmp_path / "sim.db")
    kernel = Kernel(
        {
            "storage-sqlite": {"path": db_path},
            "snapshot": {"max_file_bytes": 4096, "hash_large_files": True, "auto_capture": False},
        }
    )
    await kernel.add(SqliteStoragePlugin, SqliteSnapshotPlugin, SnapshotPlugin)
    storage: SqliteStorage = kernel.services.require("storage")
    mgr: SnapshotService = kernel.services.require("snapshot.manager")
    session = _CountingLocalSession(case_dir, virtual_root="/work/user1/case_a")

    r1 = await storage.create_run(work_dir="/work/user1/case_a", cluster_id="c1")
    res1 = await mgr.capture_run(
        session, r1.id, "/work/user1/case_a", trigger=SnapshotTrigger.MANUAL, wait_for_hash=False
    )
    assert res1.hashing_pending == 1
    assert session.read_calls.get("/work/user1/case_a/surface.dat", 0) == 0

    async def _hashed() -> bool:
        s = await mgr.get_snapshot(res1.snapshot.id)
        if s is None:
            return False
        sf = next(f for f in s.files if f.rel_path == "surface.dat")
        return sf.content_status is ContentStatus.OVERSIZE_HASHED and sf.sha256 is not None

    await _wait_for(_hashed)
    reads_after_first = session.read_calls.get("/work/user1/case_a/surface.dat", 0)
    assert reads_after_first >= 1

    # Second run in the same dir with unchanged surface.dat reuses sha256 without reading SFTP!
    r2 = await storage.create_run(work_dir="/work/user1/case_a", cluster_id="c1")
    res2 = await mgr.capture_run(
        session, r2.id, "/work/user1/case_a", trigger=SnapshotTrigger.MANUAL, wait_for_hash=True
    )
    assert res2.hashing_pending == 0
    assert session.read_calls.get("/work/user1/case_a/surface.dat", 0) == reads_after_first
    sf2 = next(f for f in res2.snapshot.files if f.rel_path == "surface.dat")
    assert sf2.content_status is ContentStatus.OVERSIZE_HASHED

    # Blob deduplication: inputs.in + solver.in stored only once across r1 and r2
    async with storage.connection.execute("SELECT COUNT(*) AS n FROM snapshot_blobs") as cur:
        row = await cur.fetchone()
        assert row is not None and int(row["n"]) == 2

    # Modify surface.dat and capture r3 -> diff detects oversize_changed
    surface.write_bytes(b"MODIFIED" * 4100)
    r3 = await storage.create_run(work_dir="/work/user1/case_a", cluster_id="c1")
    await mgr.capture_run(
        session, r3.id, "/work/user1/case_a", trigger=SnapshotTrigger.MANUAL, wait_for_hash=True
    )
    diff = await mgr.diff_runs(r3.id)
    by_path = {f.rel_path: f for f in diff.files}
    assert by_path["surface.dat"].change_kind is DiffChangeKind.OVERSIZE_CHANGED

    await kernel.stop()


async def test_diff_base_resolution_case_workdir_and_parent(tmp_path: Path) -> None:
    root = tmp_path / "work"
    d1 = root / "001_base"
    d2 = root / "002_child"
    (d1 / "system").mkdir(parents=True)
    (d1 / "constant" / "polyMesh").mkdir(parents=True)
    (d2 / "system").mkdir(parents=True)
    shutil.copy(FIXTURES / "openfoam" / "system" / "controlDict", d1 / "system" / "controlDict")
    shutil.copy(
        FIXTURES / "openfoam" / "system" / "decomposeParDict", d1 / "system" / "decomposeParDict"
    )
    (d1 / "constant" / "polyMesh" / "points").write_text("HUGE MESH", encoding="utf-8")

    # 002 changes endTime in controlDict
    (d2 / "system" / "controlDict").write_text(
        (d1 / "system" / "controlDict").read_text(encoding="utf-8").replace("5.0e-4", "2.0e-3"),
        encoding="utf-8",
    )

    kernel = Kernel(
        {"storage-sqlite": {"path": str(tmp_path / "sim.db")}, "snapshot": {"auto_capture": False}}
    )
    await kernel.add(
        SqliteStoragePlugin, SqliteCampaignPlugin, SqliteSnapshotPlugin, SnapshotPlugin
    )
    storage: Storage = kernel.services.require("storage")
    camp_store = kernel.services.require("campaign")
    mgr: SnapshotService = kernel.services.require("snapshot.manager")

    camp = await camp_store.upsert_campaign(
        CampaignSpec(project_name="p", name="c", work_root="/work")
    )
    c1, _, _ = await camp_store.upsert_case(camp.id, CaseSpec("001", work_dir="/work/001_base"))
    c2, _, _ = await camp_store.upsert_case(
        camp.id, CaseSpec("002", parent_key_raw="001", work_dir="/work/002_child")
    )
    await camp_store.set_case_parent(c2.id, c1.id)

    r1 = await storage.create_run(work_dir="/work/001_base", cluster_id="c1")
    r2 = await storage.create_run(work_dir="/work/002_child", cluster_id="c1")
    await camp_store.link_run(camp.id, c1.id, r1.id, match_kind=MatchKind.MANUAL)
    await camp_store.link_run(camp.id, c2.id, r2.id, match_kind=MatchKind.MANUAL)

    await mgr.capture_run(
        LocalRemoteSession(d1, virtual_root="/work/001_base"), r1.id, "/work/001_base"
    )
    await mgr.capture_run(
        LocalRemoteSession(d2, virtual_root="/work/002_child"), r2.id, "/work/002_child"
    )

    # polyMesh excluded from snapshot
    s1 = await mgr.list_snapshots(r1.id)
    s1_full = await mgr.get_snapshot(s1[0].id)
    assert s1_full is not None
    assert all("polyMesh" not in f.rel_path for f in s1_full.files)

    # Parent diff: 002 vs 001
    pdiff = await mgr.diff_runs(r2.id, parent=True)
    assert pdiff.base_reason == "parent_case"
    assert pdiff.base_snapshot is not None and pdiff.base_snapshot.run_id == r1.id
    cd_diff = next(f for f in pdiff.files if f.rel_path == "system/controlDict")
    assert any(sc.path == "endTime" and sc.new_value == "2.0e-3" for sc in cd_diff.semantic_changes)

    # Relaunch r3 on case 002 -> automatic base is previous_case_run (r2)
    (d2 / "system" / "controlDict").write_text(
        (d2 / "system" / "controlDict").read_text(encoding="utf-8").replace("2.0e-3", "5.0e-3"),
        encoding="utf-8",
    )
    r3 = await storage.create_run(work_dir="/work/002_child", cluster_id="c1")
    await camp_store.link_run(camp.id, c2.id, r3.id, match_kind=MatchKind.MANUAL)
    await mgr.capture_run(
        LocalRemoteSession(d2, virtual_root="/work/002_child"), r3.id, "/work/002_child"
    )
    adiff = await mgr.diff_runs(r3.id)
    assert adiff.base_reason == "previous_case_run"
    assert adiff.base_snapshot is not None and adiff.base_snapshot.run_id == r2.id

    # Workdir fallback for unlinked run r4 in same dir as r3
    r4 = await storage.create_run(work_dir="/work/002_child", cluster_id="c1")
    await mgr.capture_run(
        LocalRemoteSession(d2, virtual_root="/work/002_child"), r4.id, "/work/002_child"
    )
    wdiff = await mgr.diff_runs(r4.id)
    assert wdiff.base_reason == "previous_workdir_run"
    assert wdiff.base_snapshot is not None and wdiff.base_snapshot.run_id == r3.id

    # Run notes emit compact durable event
    long_note = "A" * 200
    await mgr.set_note(r3.id, long_note)
    got_note = await mgr.get_note(r3.id)
    assert got_note is not None and got_note.note == long_note
    evts = await storage.events(kinds=["run.note_changed"], run_id=r3.id)
    assert len(evts) == 1
    assert evts[0].payload["length"] == 200
    assert len(evts[0].payload["preview"]) == 120

    await kernel.stop()


async def test_real_monitor_triggers_queued_then_running_and_late_snapshot(tmp_path: Path) -> None:
    case_dir = tmp_path / "case_mon"
    case_dir.mkdir()
    shutil.copy(FIXTURES / "converge" / "inputs.in", case_dir / "inputs.in")
    shutil.copy(FIXTURES / "converge" / "solver_v1.in", case_dir / "solver.in")

    session = LocalRemoteSession(case_dir, cluster_id="c1", virtual_root="/work/user1/case_mon")
    profile = ClusterProfile(host="cluster.example.org", user="user1")
    sched = _FakeScheduler()
    transport = _FakeTransport({"c1": profile}, session)

    kernel = Kernel(
        {
            "storage-sqlite": {"path": str(tmp_path / "sim.db")},
            "monitor": {"loop": False},
        }
    )
    await kernel.add(
        SqliteStoragePlugin,
        SqliteSnapshotPlugin,
        SnapshotPlugin,
        MonitorPlugin,
        _make_transport_plugin(transport),
        _make_scheduler_plugin(sched),
    )

    storage: Storage = kernel.services.require("storage")
    mgr: SnapshotService = kernel.services.require("snapshot.manager")
    monitor = kernel.services.require("monitor")

    # 1. Poll while job is QUEUED -> triggers storage.event_appended -> snapshot DISCOVERED
    sched.active = [
        JobInfo(
            job_id="501",
            name="case_mon",
            state=JobState.QUEUED,
            raw_state="PENDING",
            work_dir="/work/user1/case_mon",
        )
    ]
    await monitor.poll("c1")

    async def _has_one_snap() -> bool:
        jobs = await storage.jobs(cluster_id="c1")
        if not jobs or jobs[0].run_id is None:
            return False
        return len(await mgr.list_snapshots(jobs[0].run_id)) == 1

    await _wait_for(_has_one_snap)
    job = (await storage.jobs(cluster_id="c1"))[0]
    assert job.run_id is not None
    snaps1 = await mgr.list_snapshots(job.run_id)
    assert snaps1[0].trigger is SnapshotTrigger.DISCOVERED
    assert snaps1[0].effective is True

    # 2. Modify solver.in while queued (vi on cluster!) and move job to RUNNING
    shutil.copy(FIXTURES / "converge" / "solver_v2.in", case_dir / "solver.in")
    start_t = datetime.now(UTC) - timedelta(minutes=2)
    # Set mtime of solver.in AFTER job start_time so snapshot is flagged `late`
    late_ts = (start_t + timedelta(seconds=30)).timestamp()
    os.utime(case_dir / "solver.in", (late_ts, late_ts))

    sched.active = [
        JobInfo(
            job_id="501",
            name="case_mon",
            state=JobState.RUNNING,
            raw_state="RUNNING",
            work_dir="/work/user1/case_mon",
            start_time=start_t,
        )
    ]
    await monitor.poll("c1")

    async def _has_two_snaps() -> bool:
        return len(await mgr.list_snapshots(job.run_id)) == 2  # type: ignore[arg-type]

    await _wait_for(_has_two_snaps)
    snaps2 = await mgr.list_snapshots(job.run_id)
    assert snaps2[0].effective is False
    assert snaps2[1].trigger is SnapshotTrigger.RUNNING
    assert snaps2[1].effective is True
    assert snaps2[1].late is True
    assert "solver.in" in snaps2[1].late_files

    diff_q_to_r = await mgr.diff_snapshots(snaps2[0].id, snaps2[1].id)
    assert diff_q_to_r.target_late is True
    assert "solver.in" in diff_q_to_r.target_late_files
    solver_diff = next(f for f in diff_q_to_r.files if f.rel_path == "solver.in")
    assert any(
        sc.path == "max_cfl" and sc.new_value == "0.5" for sc in solver_diff.semantic_changes
    )

    await kernel.stop()
