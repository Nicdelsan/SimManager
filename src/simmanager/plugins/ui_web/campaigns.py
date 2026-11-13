"""Campaign endpoints (M5 view) on top of the `campaign.manager` service.

GET  /api/campaigns                  list
GET  /api/campaigns/{id}             cases (computed + manual state, linked runs),
                                     never-launched cases, runs not associated
POST /api/campaigns/{id}/match       {case_key, run_id}: manual association (locked)
POST /api/campaigns/{id}/unmatch     {run_id}: remove an association (run excluded from
                                     automatic matching for this campaign)

Associations change only SimManager's own database, never the Excel file or the cluster.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import campaign as campaign_contract
from simmanager.contracts.campaign import (
    CampaignRecord,
    CampaignService,
    CaseRecord,
    CaseSummary,
    RunExecutionFacts,
)

from .api import Api, _iso, job_json, solver_json


class MatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_key: str = Field(min_length=1, max_length=200)
    run_id: int = Field(ge=1)


class UnmatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: int = Field(ge=1)


def campaign_json(c: CampaignRecord) -> dict[str, Any]:
    return {
        "id": c.id,
        "project_id": c.project_id,
        "name": c.name,
        "source_path": c.source_path,
        "sheet_name": c.sheet_name,
        "work_root": c.work_root,
        "columns": list(c.columns),
        "case_column": c.case_column,
        "manual_state_column": c.manual_state_column,
        "updated_at": _iso(c.updated_at),
    }


def facts_json(f: RunExecutionFacts) -> dict[str, Any]:
    return {
        "run_id": f.run.id,
        "origin": f.run.origin,
        "cluster_id": f.run.cluster_id,
        "work_dir": f.run.work_dir,
        "created_at": _iso(f.run.created_at),
        "job": job_json(f.job) if f.job else None,
        "solver": solver_json(f.solver_state),
        "nan_detected": f.has_nan_detected,
    }


def case_json(c: CaseRecord) -> dict[str, Any]:
    return {
        "id": c.id,
        "case_key": c.case_key,
        "row_index": c.row_index,
        "work_dir": c.work_dir,
        "manual_state": c.manual_state,
        "computed_state": str(c.computed_state),
        "params": c.params,
        "active": c.active,
        "notes": c.notes,
    }


def summary_json(s: CaseSummary) -> dict[str, Any]:
    facts = {f.run.id: f for f in s.runs}
    return {
        **case_json(s.case),
        "parent_key": s.parent_case_key,
        "children_keys": list(s.children_keys),
        "latest_run_id": s.latest.run.id if s.latest else None,
        "runs": [
            {
                **(facts_json(facts[lk.run_id]) if lk.run_id in facts else {"run_id": lk.run_id}),
                "match_kind": str(lk.match_kind),
                "match_detail": lk.match_detail,
                "locked": lk.locked,
            }
            for lk in s.links
        ],
    }


def build_campaign_router(api: Api) -> APIRouter:
    r = APIRouter()

    def manager() -> CampaignService:
        return api._need(campaign_contract.MANAGER_SERVICE_KEY)  # type: ignore[no-any-return]

    @r.get("/campaigns")
    async def campaigns() -> list[dict[str, Any]]:
        return [campaign_json(c) for c in await manager().list_campaigns()]

    @r.get("/campaigns/{campaign_id}")
    async def campaign_detail(campaign_id: int, include_inactive: bool = False) -> dict[str, Any]:
        svc = manager()
        try:
            camp, summaries = await svc.show_campaign(
                campaign_id, include_inactive=include_inactive
            )
            report = await svc.unmatched(campaign_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown campaign") from exc
        return {
            "campaign": campaign_json(camp),
            "cases": [summary_json(s) for s in summaries],
            "unlaunched": [c.case_key for c in report.unlaunched_cases],
            "unmatched_runs": [
                {
                    **facts_json(RunExecutionFacts(run=u.run, job=u.job)),
                    "ambiguous_case_keys": list(u.ambiguous_case_keys),
                }
                for u in report.unmatched_runs
            ],
        }

    @r.post("/campaigns/{campaign_id}/match")
    async def match(campaign_id: int, body: MatchBody) -> dict[str, Any]:
        try:
            link = await manager().match_run(campaign_id, body.case_key, body.run_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0]) if exc.args else "not found") from exc
        return {"case_id": link.case_id, "run_id": link.run_id, "match_kind": str(link.match_kind)}

    @r.post("/campaigns/{campaign_id}/unmatch")
    async def unmatch(campaign_id: int, body: UnmatchBody) -> dict[str, Any]:
        try:
            link = await manager().unmatch_run(campaign_id, body.run_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown campaign") from exc
        if link is None:
            raise HTTPException(404, "run not associated with this campaign")
        return {"case_id": link.case_id, "run_id": link.run_id}

    return r
