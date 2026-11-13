from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from simmanager.kernel import Kernel, PluginState
from simmanager.plugins.storage_sqlite import SqliteStorage, SqliteStoragePlugin
from simmanager.plugins.storage_sqlite.migrate import bundled_migrations


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "db" / "test.db")


async def test_append_and_read_events(db_path: str) -> None:
    storage = await SqliteStorage.open(db_path)
    first = await storage.append_event(
        "job.discovered", {"job_id": "42"}, source="t", cluster_id="c"
    )
    second = await storage.append_event("run.created", {"run": 1}, source="t", run_id=7)
    await storage.append_event("job.state_changed", {"to": "running"}, source="t", run_id=7)

    events = await storage.events()
    assert [e.id for e in events] == [first, second, second + 1]
    assert events[0].payload == {"job_id": "42"}
    assert events[0].cluster_id == "c"
    assert events[0].ts.tzinfo is not None

    assert [e.kind for e in await storage.events(since_id=first)] == [
        "run.created",
        "job.state_changed",
    ]
    assert [e.kind for e in await storage.events(kinds=["run.created"])] == ["run.created"]
    assert len(await storage.events(run_id=7)) == 2
    assert [e.id for e in await storage.latest_events(2)] == [second + 1, second]
    await storage.close()


async def test_events_are_append_only(db_path: str) -> None:
    storage = await SqliteStorage.open(db_path)
    await storage.append_event("x", source="t")
    await storage.close()

    con = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("UPDATE events SET kind = 'y'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM events")
    con.close()


async def test_migrations_applied_once(db_path: str) -> None:
    s1 = await SqliteStorage.open(db_path)
    await s1.close()
    s2 = await SqliteStorage.open(db_path)
    await s2.close()
    con = sqlite3.connect(db_path)
    versions = [r[0] for r in con.execute("SELECT version FROM schema_migrations")]
    con.close()
    assert versions == [m.version for m in bundled_migrations()]


async def test_plugin_unload_keeps_data(db_path: str) -> None:
    kernel = Kernel({"storage-sqlite": {"path": db_path}})
    await kernel.add(SqliteStoragePlugin)
    assert kernel.state("storage-sqlite") is PluginState.ACTIVE
    await kernel.services.require("storage").append_event("kept", source="t")

    await kernel.reload("storage-sqlite")
    events = await kernel.services.require("storage").events()
    assert [e.kind for e in events] == ["kept"]

    await kernel.stop()
    reopened = await SqliteStorage.open(db_path)
    assert len(await reopened.events()) == 1
    await reopened.close()


async def test_unwritable_path_fails_plugin_only(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    kernel = Kernel({"storage-sqlite": {"path": str(blocker / "sub" / "db.sqlite")}})
    await kernel.add(SqliteStoragePlugin)
    assert kernel.state("storage-sqlite") is PluginState.FAILED
    await kernel.stop()


async def test_upsert_job_creates_run_and_keeps_known_fields(db_path: str) -> None:
    from simmanager.contracts.scheduler import JobInfo, JobState

    storage = await SqliteStorage.open(db_path)
    queued = JobInfo(
        job_id="7_1",
        name="case",
        state=JobState.QUEUED,
        raw_state="PENDING",
        work_dir="/work/user1/case_a",
        stdout_path="/work/user1/case_a/out.txt",
    )
    first = await storage.upsert_job("c", queued, active=True)
    assert first.created and first.previous is None and first.record.run_id is not None

    finished = JobInfo(job_id="7_1", name="", state=JobState.COMPLETED, raw_state="COMPLETED")
    second = await storage.upsert_job("c", finished, active=False)
    assert not second.created and second.previous is not None
    assert second.previous.state is JobState.QUEUED
    assert second.record.run_id == first.record.run_id  # same run
    info = second.record.info
    assert info.state is JobState.COMPLETED and info.name == "case"
    assert info.work_dir == "/work/user1/case_a"  # unknown in the update: kept
    assert info.stdout_path == "/work/user1/case_a/out.txt"

    assert [r.info.job_id for r in await storage.jobs(active=False)] == ["7_1"]
    assert await storage.jobs(active=True) == []
    assert await storage.jobs(cluster_id="other") == []

    manual = await storage.create_run(work_dir="/work/user1/local_case", cluster_id="local")
    assert manual.id > 0
    assert manual.origin == "manual"
    assert manual.job_pk is None
    assert (await storage.run(manual.id)) == manual
    assert (await storage.run(99999)) is None
    await storage.close()


async def test_runs_job_for_run_and_live_event_hook(db_path: str) -> None:
    from simmanager.contracts.scheduler import JobInfo, JobState
    from simmanager.contracts.storage import EventRecord

    storage = await SqliteStorage.open(db_path)
    seen: list[EventRecord] = []

    async def hook(record: EventRecord) -> None:
        seen.append(record)

    storage.on_event_appended = hook
    up = await storage.upsert_job(
        "c",
        JobInfo(job_id="1", name="n", state=JobState.RUNNING, raw_state="RUNNING"),
        active=True,
    )
    manual = await storage.create_run(cluster_id="c", work_dir="/w")
    assert [r.id for r in await storage.runs()] == [manual.id, up.record.run_id]
    assert [r.origin for r in await storage.runs(cluster_id="c")] == ["manual", "discovered"]
    assert await storage.runs(cluster_id="x") == []

    job = await storage.job_for_run(up.record.run_id or 0)
    assert job is not None and job.info.job_id == "1"
    assert await storage.job_for_run(manual.id) is None

    event_id = await storage.append_event("x.y", {"a": 1}, source="t", run_id=manual.id)
    assert [(e.id, e.kind, e.payload, e.run_id) for e in seen] == [
        (event_id, "x.y", {"a": 1}, manual.id)
    ]
    await storage.close()


async def test_plugin_publishes_appended_events_on_the_bus(db_path: str) -> None:
    from simmanager.contracts import storage as contract

    kernel = Kernel({"storage-sqlite": {"path": db_path}})
    received: list[object] = []

    async def on_event(payload: object) -> None:
        received.append(payload)

    kernel.bus.on(contract.EVENT_APPENDED, on_event)
    await kernel.add(SqliteStoragePlugin)
    try:
        await kernel.services.require("storage").append_event("k", source="t")
        assert len(received) == 1 and getattr(received[0], "kind", None) == "k"
    finally:
        await kernel.stop()
