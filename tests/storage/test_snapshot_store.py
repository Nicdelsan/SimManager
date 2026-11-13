from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from simmanager.contracts.snapshot import (
    ContentStatus,
    SnapshotFileSpec,
    SnapshotStatus,
    SnapshotTrigger,
)
from simmanager.kernel import Kernel, PluginState
from simmanager.plugins.storage_sqlite import (
    SqliteSnapshotPlugin,
    SqliteSnapshotStore,
    SqliteStorage,
    SqliteStoragePlugin,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "db" / "snapshots.db")


async def test_snapshot_store_blob_dedup_effective_and_reuse_hash(db_path: str) -> None:
    kernel = Kernel({"storage-sqlite": {"path": db_path}})
    await kernel.add(SqliteStoragePlugin, SqliteSnapshotPlugin)
    assert kernel.state("snapshot-sqlite") is PluginState.ACTIVE

    storage: SqliteStorage = kernel.services.require("storage")
    store: SqliteSnapshotStore = kernel.services.require("snapshot")
    run = await storage.create_run(work_dir="/work/user1/case_a", cluster_id="cluster-a")

    data_a = b"version: 4\n---\nmax_cfl: 0.8\n"
    sha_a = hashlib.sha256(data_a).hexdigest()

    snap1 = await store.save_snapshot(
        run_id=run.id,
        trigger=SnapshotTrigger.DISCOVERED,
        effective=True,
        status=SnapshotStatus.OK,
        late=False,
        late_files=(),
        solver="converge",
        work_dir="/work/user1/case_a",
        manifest_sha256="m1",
        error_message=None,
        files=[
            SnapshotFileSpec("inputs.in", len(data_a), 100.0, sha_a, ContentStatus.STORED),
            SnapshotFileSpec(
                "surface.dat", 48_000_000, 200.0, None, ContentStatus.OVERSIZE_UNHASHED
            ),
        ],
        blobs={sha_a: (data_a, True, "utf-8")},
    )
    assert snap1.effective is True
    assert snap1.file_count == 2
    assert len(snap1.files) == 2

    # Update oversize hash in place
    updated = await store.update_file_hash(snap1.id, "surface.dat", "deadbeef" * 8)
    assert updated is True
    reused = await store.find_reusable_sha256(
        "/work/user1/case_a", "surface.dat", 48_000_000, 200.0
    )
    assert reused == "deadbeef" * 8

    # Second snapshot marks previous effective=False and deduplicates blob
    snap2 = await store.save_snapshot(
        run_id=run.id,
        trigger=SnapshotTrigger.RUNNING,
        effective=True,
        status=SnapshotStatus.OK,
        late=True,
        late_files=("inputs.in",),
        solver="converge",
        work_dir="/work/user1/case_a",
        manifest_sha256="m2",
        error_message=None,
        files=[
            SnapshotFileSpec("inputs.in", len(data_a), 100.0, sha_a, ContentStatus.STORED),
        ],
        blobs={sha_a: (data_a, True, "utf-8")},
    )
    assert snap2.effective is True
    assert snap2.late is True
    assert snap2.late_files == ("inputs.in",)

    snap1_reloaded = await store.get_snapshot(snap1.id, include_files=False)
    assert snap1_reloaded is not None
    assert snap1_reloaded.effective is False

    eff = await store.effective_snapshot(run.id)
    assert eff is not None
    assert eff.id == snap2.id

    async with storage.connection.execute("SELECT COUNT(*) AS n FROM snapshot_blobs") as cur:
        row = await cur.fetchone()
        assert row is not None and int(row["n"]) == 1

    blob = await store.get_blob(sha_a)
    assert blob is not None
    assert blob.data == data_a

    # Lifecycle reload preserves data
    await kernel.reload("snapshot-sqlite")
    reloaded_store = kernel.services.require("snapshot")
    note = await reloaded_store.set_note(run.id, "Lowered CFL")
    assert note.note == "Lowered CFL"
    got_note = await reloaded_store.get_note(run.id)
    assert got_note is not None and got_note.note == "Lowered CFL"

    await kernel.stop()
