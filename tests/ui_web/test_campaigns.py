"""Campaign endpoints of the web UI on top of `campaign.manager` (M5 backend)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from simmanager.contracts import campaign as campaign_contract
from simmanager.contracts import monitor as monitor_contract
from simmanager.contracts.campaign import CampaignSpec, CaseSpec
from simmanager.contracts.monitor import PollReport
from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.contracts.storage import Storage
from simmanager.plugins.campaign_excel import CampaignExcelPlugin
from simmanager.plugins.storage_sqlite import (
    SqliteCampaignPlugin,
    SqliteStoragePlugin,
    SqliteTimeSeriesPlugin,
)

from .webclient import Web, running_web

ROOT = "/work/user1/proj_a"


@pytest.fixture
async def web(tmp_path: Path) -> AsyncIterator[Web]:
    config = {"storage-sqlite": {"path": str(tmp_path / "sim.db")}}
    async with running_web(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        SqliteCampaignPlugin,
        CampaignExcelPlugin,
        config=config,
    ) as w:
        yield w


async def _setup(web: Web) -> tuple[int, int, int]:
    """Campaign with 3 cases: case 1 matched automatically, case 2 and 3 never launched;
    one discovered run in another directory stays unassociated."""
    store = web.kernel.services.require(campaign_contract.SERVICE_KEY)
    storage: Storage = web.kernel.services.require("storage")
    camp = await store.upsert_campaign(
        CampaignSpec(
            project_name="p",
            name="sweep",
            work_root=ROOT,
            manual_state_column="State",
            columns=("# Case", "Species", "State"),
        )
    )
    for key, state, species in (("1", "Running", "CH3OH"), ("2", "Pending", "N2"), ("3", "", "N2")):
        await store.upsert_case(
            camp.id,
            CaseSpec(
                case_key=key,
                work_dir=f"{ROOT}/00{key}_case",
                manual_state=state or None,
                params={"Species": species, "State": state},
            ),
        )
    matched = await storage.upsert_job(
        "cl",
        JobInfo(
            job_id="11",
            name="c1",
            state=JobState.RUNNING,
            raw_state="RUNNING",
            work_dir=f"{ROOT}/001_case",
        ),
        active=True,
    )
    stray = await storage.upsert_job(
        "cl",
        JobInfo(
            job_id="12",
            name="other",
            state=JobState.QUEUED,
            raw_state="PENDING",
            work_dir="/work/user1/elsewhere/x",
        ),
        active=True,
    )
    await web.kernel.bus.emit(
        monitor_contract.POLLED_EVENT, PollReport(cluster_id="cl", ok=True, discovered=2)
    )
    assert matched.record.run_id and stray.record.run_id
    return camp.id, matched.record.run_id, stray.record.run_id


async def _get(web: Web, path: str) -> Any:
    async with web.client() as c:
        r = await c.get(path)
    assert r.status_code == 200, (path, r.text)
    return r.json()


def _case(detail: dict[str, Any], key: str) -> dict[str, Any]:
    return next(c for c in detail["cases"] if c["case_key"] == key)


async def test_campaign_list_and_detail(web: Web) -> None:
    camp_id, matched_run, stray_run = await _setup(web)
    listing = await _get(web, "/api/campaigns")
    assert [(c["id"], c["name"], c["work_root"]) for c in listing] == [(camp_id, "sweep", ROOT)]

    detail = await _get(web, f"/api/campaigns/{camp_id}")
    case1 = _case(detail, "1")
    assert case1["computed_state"] == "running" and case1["manual_state"] == "Running"
    assert case1["params"]["Species"] == "CH3OH"
    assert [r["run_id"] for r in case1["runs"]] == [matched_run]
    assert case1["runs"][0]["job"]["job_id"] == "11"
    assert case1["runs"][0]["match_kind"].startswith("auto_") and not case1["runs"][0]["locked"]
    assert _case(detail, "2")["computed_state"] == "planned"
    assert _case(detail, "2")["manual_state"] == "Pending"
    assert detail["unlaunched"] == ["2", "3"]
    assert [r["run_id"] for r in detail["unmatched_runs"]] == [stray_run]
    assert detail["unmatched_runs"][0]["job"]["job_id"] == "12"


async def test_manual_association_and_removal(web: Web) -> None:
    camp_id, _matched_run, stray_run = await _setup(web)
    r = await web.post_json(
        f"/api/campaigns/{camp_id}/match", {"case_key": "2", "run_id": stray_run}
    )
    assert r.status_code == 200, r.text
    detail = await _get(web, f"/api/campaigns/{camp_id}")
    case2 = _case(detail, "2")
    assert [x["run_id"] for x in case2["runs"]] == [stray_run]
    assert case2["runs"][0]["locked"] and case2["runs"][0]["match_kind"] == "manual"
    assert case2["computed_state"] == "queued"
    assert detail["unlaunched"] == ["3"] and detail["unmatched_runs"] == []

    r = await web.post_json(f"/api/campaigns/{camp_id}/unmatch", {"run_id": stray_run})
    assert r.status_code == 200
    detail = await _get(web, f"/api/campaigns/{camp_id}")
    assert _case(detail, "2")["runs"] == [] and _case(detail, "2")["computed_state"] == "planned"
    assert [x["run_id"] for x in detail["unmatched_runs"]] == [stray_run]

    storage: Storage = web.kernel.services.require("storage")
    kinds = [e.kind for e in await storage.events(run_id=stray_run)]
    assert "case.matched" in kinds and "case.unmatched" in kinds


async def test_campaign_errors(web: Web) -> None:
    camp_id, _, stray_run = await _setup(web)
    async with web.client() as c:
        assert (await c.get("/api/campaigns/999")).status_code == 404
    bad = [
        (f"/api/campaigns/{camp_id}/match", {"case_key": "nope", "run_id": stray_run}, 404),
        (f"/api/campaigns/{camp_id}/match", {"case_key": "2", "run_id": 99999}, 404),
        (f"/api/campaigns/{camp_id}/match", {"case_key": "2"}, 422),
        (f"/api/campaigns/{camp_id}/unmatch", {"run_id": stray_run}, 404),  # not associated
        ("/api/campaigns/999/unmatch", {"run_id": stray_run}, 404),
    ]
    for path, body, status in bad:
        r = await web.post_json(path, body)
        assert r.status_code == status, (path, body, r.text)
    # Same Origin/JSON rules as every state-changing request.
    async with web.client() as c:
        r = await c.post(
            f"/api/campaigns/{camp_id}/match", json={"case_key": "2", "run_id": stray_run}
        )
        assert r.status_code == 403


async def test_campaigns_without_service_answer_503() -> None:
    async with running_web() as web, web.client() as c:
        assert (await c.get("/api/campaigns")).status_code == 503
