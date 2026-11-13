from __future__ import annotations

from pathlib import Path

import pytest

from simmanager.contracts.campaign import (
    CampaignSpec,
    CaseComputedState,
    CaseSpec,
    MatchKind,
)
from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.contracts.timeseries import RunSolverState, SolverState
from simmanager.kernel import Kernel, PluginState
from simmanager.plugins.storage_sqlite import (
    SqliteCampaignPlugin,
    SqliteCampaignStore,
    SqliteStorage,
    SqliteStoragePlugin,
    SqliteTimeSeriesStore,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "db" / "campaign.db")


async def test_campaign_plugin_lifecycle_preserves_data(db_path: str) -> None:
    kernel = Kernel({"storage-sqlite": {"path": db_path}})
    await kernel.add(SqliteStoragePlugin, SqliteCampaignPlugin)
    assert kernel.state("campaign-sqlite") is PluginState.ACTIVE

    store = kernel.services.require("campaign")
    camp = await store.upsert_campaign(
        CampaignSpec(project_name="proj-a", name="camp-1", work_root="/work/user1/proj-a")
    )
    await store.upsert_case(camp.id, CaseSpec(case_key="1", work_dir="/work/user1/proj-a/001"))

    await kernel.reload("campaign-sqlite")
    reloaded = await kernel.services.require("campaign").campaign("camp-1", project_name="proj-a")
    assert reloaded is not None
    assert reloaded.work_root == "/work/user1/proj-a"
    await kernel.stop()


async def test_campaign_and_case_crud_rename_and_ambiguity(db_path: str) -> None:
    storage = await SqliteStorage.open(db_path)
    store = SqliteCampaignStore(storage.connection, storage.write_lock)

    c1 = await store.upsert_campaign(CampaignSpec(project_name="p1", name="shared"))
    c2 = await store.upsert_campaign(CampaignSpec(project_name="p2", name="shared"))
    assert c1.project_id != c2.project_id

    with pytest.raises(ValueError, match="multiple campaigns"):
        await store.campaign("shared")
    assert (await store.campaign("shared", project_name="p2")) == c2
    assert (await store.campaign(c1.id)) == c1

    rec1, created, changed = await store.upsert_case(
        c1.id,
        CaseSpec(
            case_key="1",
            row_index=2,
            work_dir="/work/user1/p1/001_base",
            manual_state="Done",
            params={"# Case": "1", "VOF": "Yes - Cav. (HRM)"},
        ),
    )
    assert created and changed
    assert rec1.computed_state is CaseComputedState.PLANNED

    rec1_same, created2, changed2 = await store.upsert_case(
        c1.id,
        CaseSpec(
            case_key="1",
            row_index=2,
            work_dir="/work/user1/p1/001_base",
            manual_state="Done",
            params={"# Case": "1", "VOF": "Yes - Cav. (HRM)"},
        ),
    )
    assert not created2 and not changed2
    assert rec1_same.id == rec1.id

    rec2, _, _ = await store.upsert_case(
        c1.id, CaseSpec(case_key="2", row_index=3, parent_key_raw="1")
    )
    await store.set_case_parent(rec2.id, rec1.id)
    got2 = await store.case(c1.id, "2")
    assert got2 is not None and got2.parent_case_id == rec1.id

    renamed = await store.rename_case(c1.id, "2", "2b")
    assert renamed.id == rec2.id and renamed.case_key == "2b"
    assert await store.case(c1.id, "2") is None

    await store.set_cases_inactive(c1.id, ["2b"])
    assert [c.case_key for c in await store.cases(c1.id)] == ["1"]
    assert [c.case_key for c in await store.cases(c1.id, include_inactive=True)] == ["1", "2b"]

    prev = await store.update_case_computed_state(rec1.id, CaseComputedState.RUNNING)
    assert prev is CaseComputedState.PLANNED
    assert await store.update_case_computed_state(rec1.id, CaseComputedState.RUNNING) is None

    await storage.close()


async def test_run_links_manual_lock_exclusion_ambiguity_and_facts(db_path: str) -> None:
    storage = await SqliteStorage.open(db_path)
    ts = SqliteTimeSeriesStore(storage.connection, storage.write_lock)
    store = SqliteCampaignStore(storage.connection, storage.write_lock)

    camp = await store.upsert_campaign(CampaignSpec(project_name="p", name="c"))
    case1, _, _ = await store.upsert_case(camp.id, CaseSpec(case_key="1"))
    case2, _, _ = await store.upsert_case(camp.id, CaseSpec(case_key="2"))

    up = await storage.upsert_job(
        "cluster-a",
        JobInfo(
            job_id="101",
            name="001_case",
            state=JobState.FAILED,
            raw_state="FAILED",
            work_dir="/work/user1/001_case",
        ),
        active=False,
    )
    run_id = up.record.run_id
    assert run_id is not None
    await ts.upsert_run_solver_state(
        RunSolverState(
            run_id=run_id,
            solver="converge",
            work_dir="/work/user1/001_case",
            solver_state=SolverState.FAILED,
        )
    )
    await storage.append_event(
        "solver.nan_detected", {"column": "Pressure"}, source="solver-ingest", run_id=run_id
    )

    assert await store.record_ambiguity(camp.id, run_id, [case2.id, case1.id]) is True
    assert await store.record_ambiguity(camp.id, run_id, [case1.id, case2.id]) is False
    ambs = await store.ambiguities_for_campaign(camp.id)
    assert len(ambs) == 1 and ambs[0].candidate_case_ids == (case1.id, case2.id)

    link = await store.link_run(
        camp.id, case1.id, run_id, match_kind=MatchKind.MANUAL, match_detail="cli"
    )
    assert link.locked is True
    assert await store.ambiguities_for_campaign(camp.id) == []

    # Auto link must never overwrite a manual locked link.
    kept = await store.link_run(
        camp.id, case2.id, run_id, match_kind=MatchKind.AUTO_REGEX, locked=False
    )
    assert kept.case_id == case1.id and kept.match_kind is MatchKind.MANUAL

    removed = await store.unlink_run(camp.id, run_id, exclude=True)
    assert removed is not None
    assert await store.is_run_excluded(camp.id, run_id) is True
    assert await store.links_for_campaign(camp.id) == []

    # Re-linking manually clears the exclusion.
    await store.link_run(camp.id, case2.id, run_id, match_kind=MatchKind.MANUAL)
    assert await store.is_run_excluded(camp.id, run_id) is False

    facts = await store.run_facts([run_id])
    assert run_id in facts
    assert facts[run_id].job is not None and facts[run_id].job.info.job_id == "101"
    assert facts[run_id].solver_state is not None
    assert facts[run_id].has_nan_detected is True

    await storage.close()
