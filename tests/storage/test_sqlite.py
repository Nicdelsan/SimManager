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
