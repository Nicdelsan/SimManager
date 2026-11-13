"""`campaign-excel` plugin: Excel import/export, run matching on `monitor.polled`, state sync."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import campaign as contract
from simmanager.contracts import monitor as monitor_contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts import timeseries as ts_contract
from simmanager.contracts.campaign import (
    DEFAULT_NAMING_REGEX,
    DURABLE_EVENT_KINDS,
    EVENT_AMBIGUOUS_MATCH,
    EVENT_CAMPAIGN_IMPORTED,
    EVENT_CASE_MATCHED,
    EVENT_CASE_STATE_CHANGED,
    EVENT_CASE_UNMATCHED,
    CampaignRecord,
    CampaignSpec,
    CampaignStore,
    CaseRecord,
    CaseRunLink,
    CaseSummary,
    ExcelPreview,
    ExportOptions,
    ExportReport,
    ImportOptions,
    ImportReport,
    MatchKind,
    ReconcileReport,
    RunExecutionFacts,
    UnmatchedReport,
    UnmatchedRunInfo,
)
from simmanager.contracts.monitor import PollReport
from simmanager.contracts.storage import JobRecord, Storage
from simmanager.contracts.timeseries import TimeSeriesStore
from simmanager.kernel import Context, Plugin

from .excel import parse_workbook, preview_workbook, write_enriched_workbook
from .matching import (
    compute_case_state,
    keys_equivalent,
    match_run_to_cases,
    normalize_work_dir,
    parse_monitor_selector,
    select_governing_run,
    select_series_value,
)


class CampaignExcelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    naming_regex: str = DEFAULT_NAMING_REGEX
    export_monitors: list[str] = Field(default_factory=list)
    """Monitor selectors (`file:series[:qualifier]`) appended as `SM_Monitor_*` on export."""


class CampaignManager:
    """Implements `simmanager.contracts.campaign.CampaignService`."""

    def __init__(
        self,
        ctx: Context,
        storage: Storage,
        timeseries: TimeSeriesStore,
        store: CampaignStore,
        cfg: CampaignExcelConfig,
    ) -> None:
        self._ctx = ctx
        self._storage = storage
        self._ts = timeseries
        self._store = store
        self._cfg = cfg

    async def preview_xlsx(
        self,
        xlsx_path: Path,
        *,
        sheet_name: str | None = None,
        header_row: int | None = None,
    ) -> ExcelPreview:
        return await asyncio.to_thread(
            preview_workbook, xlsx_path, sheet_name=sheet_name, header_row=header_row
        )

    async def import_xlsx(
        self,
        xlsx_path: Path,
        options: ImportOptions | None = None,
    ) -> ImportReport:
        opts = options or ImportOptions()
        naming_regex = (
            opts.naming_regex if opts.naming_regex is not None else self._cfg.naming_regex
        )
        resolved_path = await asyncio.to_thread(xlsx_path.resolve)
        parsed = await asyncio.to_thread(
            parse_workbook,
            resolved_path,
            sheet_name=opts.sheet_name,
            header_row=opts.header_row,
            case_column=opts.case_column,
            work_dir_column=opts.work_dir_column,
            manual_state_column=opts.manual_state_column,
            naming_regex=naming_regex,
            stop_on_blank_case=opts.stop_on_blank_case,
        )
        campaign_name = opts.campaign_name or resolved_path.stem

        existing_campaign = await self._store.campaign(
            campaign_name, project_name=opts.project_name
        )
        explicit_root = normalize_work_dir(opts.work_root)
        chosen_root = (
            explicit_root
            or parsed.inferred_work_root
            or (existing_campaign.work_root if existing_campaign else None)
        )

        if opts.dry_run:
            return await self._preview_import_diff(
                existing_campaign,
                opts.project_name,
                campaign_name,
                parsed.cases,
                apply_renames=opts.apply_renames,
            )

        campaign = await self._store.upsert_campaign(
            CampaignSpec(
                project_name=opts.project_name,
                name=campaign_name,
                source_path=str(resolved_path),
                sheet_name=parsed.sheet_name,
                header_row=parsed.header_row,
                case_column=parsed.case_column,
                work_dir_column=parsed.work_dir_column,
                manual_state_column=parsed.manual_state_column,
                work_root=chosen_root,
                naming_regex=naming_regex,
                columns=parsed.columns,
            )
        )

        existing_cases = await self._store.cases(campaign.id, include_inactive=True)
        existing_by_key = {c.case_key: c for c in existing_cases}
        new_keys = {spec.case_key for spec in parsed.cases}

        # Detect candidate renames: missing active case whose work_dir uniquely matches a new case.
        disappeared = [c for c in existing_cases if c.active and c.case_key not in new_keys]
        by_wd: dict[str, list[CaseRecord]] = {}
        for c in disappeared:
            if c.work_dir:
                by_wd.setdefault(c.work_dir, []).append(c)

        new_specs_by_wd: dict[str, list[str]] = {}
        for spec in parsed.cases:
            if spec.case_key not in existing_by_key and spec.work_dir:
                new_specs_by_wd.setdefault(spec.work_dir, []).append(spec.case_key)

        renames_detected: list[tuple[str, str]] = []
        rename_map: dict[str, str] = {}
        for wd, old_list in by_wd.items():
            new_list = new_specs_by_wd.get(wd, [])
            if len(old_list) == 1 and len(new_list) == 1:
                old_key = old_list[0].case_key
                new_key = new_list[0]
                renames_detected.append((old_key, new_key))
                rename_map[new_key] = old_key

        if opts.apply_renames:
            for old_key, new_key in renames_detected:
                await self._store.rename_case(campaign.id, old_key, new_key)

        added: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        renamed_old_keys = {old for old, _ in renames_detected} if opts.apply_renames else set()

        for spec in parsed.cases:
            _, created, changed = await self._store.upsert_case(campaign.id, spec, active=True)
            if spec.case_key in rename_map and opts.apply_renames:
                continue
            if created:
                added.append(spec.case_key)
            elif changed:
                updated.append(spec.case_key)
            else:
                unchanged.append(spec.case_key)

        missing = [
            c.case_key
            for c in disappeared
            if c.case_key not in renamed_old_keys
            and not (c.work_dir and c.work_dir in new_specs_by_wd and opts.apply_renames)
        ]
        # Only mark inactive if not renamed away.
        to_deactivate = [c.case_key for c in disappeared if c.case_key not in renamed_old_keys]
        await self._store.set_cases_inactive(campaign.id, to_deactivate)

        # Second pass: resolve parent_case_id derivation links within the campaign.
        all_active = await self._store.cases(campaign.id, include_inactive=False)
        for c in all_active:
            parent_id: int | None = None
            if c.parent_key_raw:
                parent_id = _resolve_parent_id(c.parent_key_raw, all_active, exclude_id=c.id)
            if parent_id != c.parent_case_id:
                await self._store.set_case_parent(c.id, parent_id)

        rec_report = await self._reconcile_campaign(campaign)

        await self._record_event(
            EVENT_CAMPAIGN_IMPORTED,
            {
                "campaign_id": campaign.id,
                "campaign_name": campaign.name,
                "project_name": opts.project_name,
                "added": len(added),
                "updated": len(updated),
                "unchanged": len(unchanged),
                "missing": len(missing),
                "renamed": len(renames_detected),
                "work_root": campaign.work_root,
            },
        )

        return ImportReport(
            campaign_id=campaign.id,
            project_name=opts.project_name,
            campaign_name=campaign.name,
            added=tuple(added),
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            missing=tuple(missing),
            renamed=tuple(renames_detected),
            matched_runs=rec_report.matched,
            ambiguous_runs=rec_report.ambiguous,
            dry_run=False,
        )

    async def _preview_import_diff(
        self,
        existing_campaign: CampaignRecord | None,
        project_name: str,
        campaign_name: str,
        specs: Sequence[Any],
        *,
        apply_renames: bool,
    ) -> ImportReport:
        if existing_campaign is None:
            return ImportReport(
                campaign_id=None,
                project_name=project_name,
                campaign_name=campaign_name,
                added=tuple(s.case_key for s in specs),
                dry_run=True,
            )
        existing_cases = await self._store.cases(existing_campaign.id, include_inactive=True)
        existing_by_key = {c.case_key: c for c in existing_cases}
        new_keys = {s.case_key for s in specs}
        disappeared = [c for c in existing_cases if c.active and c.case_key not in new_keys]
        by_wd: dict[str, list[CaseRecord]] = {}
        for c in disappeared:
            if c.work_dir:
                by_wd.setdefault(c.work_dir, []).append(c)
        new_by_wd: dict[str, list[str]] = {}
        for s in specs:
            if s.case_key not in existing_by_key and s.work_dir:
                new_by_wd.setdefault(s.work_dir, []).append(s.case_key)
        renamed: list[tuple[str, str]] = []
        for wd, old_l in by_wd.items():
            new_l = new_by_wd.get(wd, [])
            if len(old_l) == 1 and len(new_l) == 1:
                renamed.append((old_l[0].case_key, new_l[0]))
        renamed_new = {n for _, n in renamed} if apply_renames else set()
        renamed_old = {o for o, _ in renamed} if apply_renames else set()

        added: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        for s in specs:
            if s.case_key in renamed_new:
                continue
            old = existing_by_key.get(s.case_key)
            if old is None:
                added.append(s.case_key)
            elif (
                old.row_index != s.row_index
                or old.parent_key_raw != s.parent_key_raw
                or old.work_dir != s.work_dir
                or old.manual_state != s.manual_state
                or old.params != dict(s.params)
                or not old.active
            ):
                updated.append(s.case_key)
            else:
                unchanged.append(s.case_key)
        missing = [c.case_key for c in disappeared if c.case_key not in renamed_old]
        return ImportReport(
            campaign_id=existing_campaign.id,
            project_name=project_name,
            campaign_name=campaign_name,
            added=tuple(added),
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            missing=tuple(missing),
            renamed=tuple(renamed),
            dry_run=True,
        )

    async def list_campaigns(self, *, project_name: str | None = None) -> list[CampaignRecord]:
        return await self._store.campaigns(project_name=project_name)

    async def show_campaign(
        self,
        campaign: str | int,
        *,
        project_name: str | None = None,
        include_inactive: bool = False,
    ) -> tuple[CampaignRecord, list[CaseSummary]]:
        camp = await self._require_campaign(campaign, project_name=project_name)
        cases = await self._store.cases(camp.id, include_inactive=include_inactive)
        links = await self._store.links_for_campaign(camp.id)
        facts = await self._store.run_facts([lk.run_id for lk in links])

        by_id = {c.id: c for c in await self._store.cases(camp.id, include_inactive=True)}
        children_by_parent: dict[int, list[str]] = {}
        for c in cases:
            if c.parent_case_id is not None:
                children_by_parent.setdefault(c.parent_case_id, []).append(c.case_key)

        links_by_case: dict[int, list[CaseRunLink]] = {}
        for lk in links:
            links_by_case.setdefault(lk.case_id, []).append(lk)

        summaries = [
            _build_case_summary(
                c,
                links_by_case.get(c.id, []),
                facts,
                parent_key=by_id[c.parent_case_id].case_key if c.parent_case_id in by_id else None,
                children_keys=children_by_parent.get(c.id, []),
            )
            for c in cases
        ]
        return camp, summaries

    async def show_case(
        self,
        campaign: str | int,
        case_key: str,
        *,
        project_name: str | None = None,
    ) -> tuple[CampaignRecord, CaseSummary]:
        camp, summaries = await self.show_campaign(
            campaign, project_name=project_name, include_inactive=True
        )
        for s in summaries:
            if s.case.case_key == case_key:
                return camp, s
        raise KeyError(f"case {case_key!r} not found in campaign {camp.name!r}")

    async def match_run(
        self,
        campaign: str | int,
        case_key: str,
        run_id: int,
        *,
        project_name: str | None = None,
    ) -> CaseRunLink:
        camp = await self._require_campaign(campaign, project_name=project_name)
        case = await self._store.case(camp.id, case_key)
        if case is None:
            raise KeyError(f"case {case_key!r} not found in campaign {camp.name!r}")
        facts = await self._store.run_facts([run_id])
        if run_id not in facts:
            raise KeyError(f"run {run_id} not found")

        existing_links = await self._store.links_for_campaign(camp.id)
        prev_case_id = next((lk.case_id for lk in existing_links if lk.run_id == run_id), None)

        link = await self._store.link_run(
            camp.id,
            case.id,
            run_id,
            match_kind=MatchKind.MANUAL,
            match_detail="manual",
            locked=True,
        )
        run_rec = facts[run_id].run
        await self._record_event(
            EVENT_CASE_MATCHED,
            {
                "campaign_id": camp.id,
                "case_id": case.id,
                "case_key": case.case_key,
                "run_id": run_id,
                "match_kind": str(MatchKind.MANUAL),
            },
            cluster_id=run_rec.cluster_id,
            run_id=run_id,
        )
        await self._recompute_case_state(camp, case)
        if prev_case_id is not None and prev_case_id != case.id:
            prev_case = await self._store.case(camp.id, prev_case_id)
            if prev_case is not None:
                await self._recompute_case_state(camp, prev_case)
        return link

    async def unmatch_run(
        self,
        campaign: str | int,
        run_id: int,
        *,
        project_name: str | None = None,
    ) -> CaseRunLink | None:
        camp = await self._require_campaign(campaign, project_name=project_name)
        removed = await self._store.unlink_run(camp.id, run_id, exclude=True)
        if removed is None:
            return None
        case = await self._store.case(camp.id, removed.case_id)
        facts = await self._store.run_facts([run_id])
        run_rec = facts[run_id].run if run_id in facts else None
        await self._record_event(
            EVENT_CASE_UNMATCHED,
            {
                "campaign_id": camp.id,
                "case_id": removed.case_id,
                "case_key": case.case_key if case else None,
                "run_id": run_id,
            },
            cluster_id=run_rec.cluster_id if run_rec else None,
            run_id=run_id,
        )
        if case is not None:
            await self._recompute_case_state(camp, case)
        return removed

    async def unmatched(
        self,
        campaign: str | int,
        *,
        project_name: str | None = None,
    ) -> UnmatchedReport:
        camp = await self._require_campaign(campaign, project_name=project_name)
        cases = await self._store.cases(camp.id, include_inactive=False)
        links = await self._store.links_for_campaign(camp.id)
        linked_case_ids = {lk.case_id for lk in links}
        linked_run_ids = {lk.run_id for lk in links}

        unlaunched = tuple(c for c in cases if c.id not in linked_case_ids)

        all_runs = await self._store.list_runs(limit=1000)
        unmatched_run_ids = [r.id for r in all_runs if r.id not in linked_run_ids]
        facts = await self._store.run_facts(unmatched_run_ids)
        ambiguities = {a.run_id: a for a in await self._store.ambiguities_for_campaign(camp.id)}
        case_key_by_id = {c.id: c.case_key for c in cases}

        unmatched_runs = tuple(
            UnmatchedRunInfo(
                run=r,
                job=facts[r.id].job if r.id in facts else None,
                ambiguous_case_keys=tuple(
                    case_key_by_id[cid]
                    for cid in ambiguities[r.id].candidate_case_ids
                    if cid in case_key_by_id
                )
                if r.id in ambiguities
                else (),
            )
            for r in all_runs
            if r.id not in linked_run_ids
        )
        return UnmatchedReport(
            campaign=camp,
            unlaunched_cases=unlaunched,
            unmatched_runs=unmatched_runs,
        )

    async def reconcile_all(self, *, cluster_id: str | None = None) -> ReconcileReport:
        campaigns = await self._store.campaigns()
        total_matched = 0
        total_ambiguous = 0
        total_changes = 0
        for camp in campaigns:
            rep = await self._reconcile_campaign(camp, cluster_id=cluster_id)
            total_matched += rep.matched
            total_ambiguous += rep.ambiguous
            total_changes += rep.state_changes
        return ReconcileReport(
            campaigns_checked=len(campaigns),
            matched=total_matched,
            ambiguous=total_ambiguous,
            state_changes=total_changes,
        )

    async def _reconcile_campaign(
        self, camp: CampaignRecord, *, cluster_id: str | None = None
    ) -> ReconcileReport:
        cases = await self._store.cases(camp.id, include_inactive=False)
        links = await self._store.links_for_campaign(camp.id)
        linked_run_ids = {lk.run_id for lk in links}
        excluded_run_ids = await self._store.exclusions_for_campaign(camp.id)

        all_runs = await self._store.list_runs(cluster_id=cluster_id, limit=1000)
        candidate_runs = [
            r for r in all_runs if r.id not in linked_run_ids and r.id not in excluded_run_ids
        ]
        run_facts = await self._store.run_facts([r.id for r in candidate_runs])

        matched_count = 0
        ambiguous_count = 0
        for run in candidate_runs:
            job: JobRecord | None = run_facts[run.id].job if run.id in run_facts else None
            kind, detail, candidates = match_run_to_cases(
                run,
                job,
                cases,
                work_root=camp.work_root,
                naming_regex=camp.naming_regex,
            )
            if len(candidates) == 1 and kind is not None:
                target = candidates[0]
                await self._store.link_run(
                    camp.id,
                    target.id,
                    run.id,
                    match_kind=kind,
                    match_detail=detail,
                    locked=False,
                )
                matched_count += 1
                await self._record_event(
                    EVENT_CASE_MATCHED,
                    {
                        "campaign_id": camp.id,
                        "case_id": target.id,
                        "case_key": target.case_key,
                        "run_id": run.id,
                        "match_kind": str(kind),
                        "match_detail": detail,
                    },
                    cluster_id=run.cluster_id,
                    run_id=run.id,
                )
            elif len(candidates) > 1:
                ambiguous_count += 1
                cand_ids = [c.id for c in candidates]
                is_new = await self._store.record_ambiguity(camp.id, run.id, cand_ids)
                if is_new:
                    await self._record_event(
                        EVENT_AMBIGUOUS_MATCH,
                        {
                            "campaign_id": camp.id,
                            "run_id": run.id,
                            "match_kind": str(kind),
                            "candidate_cases": [c.case_key for c in candidates],
                        },
                        cluster_id=run.cluster_id,
                        run_id=run.id,
                    )

        # Recompute computed_state for all active cases of this campaign.
        updated_links = await self._store.links_for_campaign(camp.id)
        all_facts = await self._store.run_facts([lk.run_id for lk in updated_links])
        facts_by_case: dict[int, list[RunExecutionFacts]] = {}
        for lk in updated_links:
            if lk.run_id in all_facts:
                facts_by_case.setdefault(lk.case_id, []).append(all_facts[lk.run_id])

        state_changes = 0
        for case in cases:
            changed = await self._recompute_case_state(
                camp, case, facts=facts_by_case.get(case.id, [])
            )
            if changed:
                state_changes += 1

        return ReconcileReport(
            campaigns_checked=1,
            matched=matched_count,
            ambiguous=ambiguous_count,
            state_changes=state_changes,
        )

    async def _recompute_case_state(
        self,
        camp: CampaignRecord,
        case: CaseRecord,
        *,
        facts: list[RunExecutionFacts] | None = None,
    ) -> bool:
        if facts is None:
            links = [
                lk for lk in await self._store.links_for_campaign(camp.id) if lk.case_id == case.id
            ]
            facts_map = await self._store.run_facts([lk.run_id for lk in links])
            facts = [facts_map[lk.run_id] for lk in links if lk.run_id in facts_map]

        new_state = compute_case_state(facts)
        prev = await self._store.update_case_computed_state(case.id, new_state)
        if prev is None:
            return False

        gov = select_governing_run(facts)
        await self._record_event(
            EVENT_CASE_STATE_CHANGED,
            {
                "campaign_id": camp.id,
                "case_id": case.id,
                "case_key": case.case_key,
                "from": str(prev),
                "to": str(new_state),
            },
            cluster_id=gov.run.cluster_id if gov else None,
            run_id=gov.run.id if gov else None,
        )
        return True

    async def export_xlsx(
        self,
        campaign: str | int,
        options: ExportOptions | None = None,
        *,
        project_name: str | None = None,
    ) -> ExportReport:
        opts = options or ExportOptions()
        camp, summaries = await self.show_campaign(
            campaign, project_name=project_name, include_inactive=opts.include_inactive
        )
        out_path = opts.output_path
        if out_path is None:
            if camp.source_path:
                src = Path(camp.source_path)
                out_path = src.with_name(f"{src.stem}_simmanager.xlsx")
            else:
                out_path = Path(f"{camp.name}_simmanager.xlsx")

        raw_monitors = (
            list(opts.monitors)
            if opts.monitors is not None
            else (list(camp.export_monitors) or list(self._cfg.export_monitors))
        )
        selectors = [parse_monitor_selector(m) for m in raw_monitors if m.strip()]
        monitor_labels = [sel.raw for sel in selectors]

        monitor_values: dict[int, dict[str, float | None]] = {}
        if selectors:
            for s in summaries:
                if s.latest is None:
                    continue
                series = await self._ts.list_series(s.latest.run.id)
                monitor_values[s.case.id] = {
                    sel.raw: select_series_value(series, sel) for sel in selectors
                }

        used_template, added_cols = await asyncio.to_thread(
            write_enriched_workbook,
            camp,
            summaries,
            monitor_values,
            monitor_labels,
            out_path,
        )
        return ExportReport(
            campaign_id=camp.id,
            campaign_name=camp.name,
            output_path=out_path,
            cases_exported=len(summaries),
            used_template=used_template,
            columns_added=added_cols,
        )

    async def on_monitor_polled(self, payload: Any) -> None:
        if isinstance(payload, PollReport):
            if not payload.ok:
                return
            cluster_id = payload.cluster_id
        elif isinstance(payload, Mapping):
            if not payload.get("ok"):
                return
            cid = payload.get("cluster_id")
            if not isinstance(cid, str):
                return
            cluster_id = cid
        else:
            return
        await self.reconcile_all(cluster_id=cluster_id)

    async def on_run_event(self, payload: Any) -> None:
        """Immediately recompute linked cases' states when solver events arrive."""
        if not isinstance(payload, Mapping):
            return
        run_id = payload.get("run_id")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            return
        for lk in await self._store.links_for_run(run_id):
            camp = await self._store.campaign(lk.campaign_id)
            case = await self._store.case(lk.campaign_id, lk.case_id)
            if camp is not None and case is not None and case.active:
                await self._recompute_case_state(camp, case)

    async def _require_campaign(
        self, campaign: str | int, *, project_name: str | None = None
    ) -> CampaignRecord:
        found = await self._store.campaign(campaign, project_name=project_name)
        if found is None:
            raise KeyError(f"campaign {campaign!r} not found")
        return found

    async def _record_event(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        cluster_id: str | None = None,
        run_id: int | None = None,
    ) -> None:
        if kind in DURABLE_EVENT_KINDS:
            await self._storage.append_event(
                kind,
                payload,
                source=self._ctx.plugin_name,
                cluster_id=cluster_id,
                run_id=run_id,
            )
        await self._ctx.emit(kind, {"cluster_id": cluster_id, "run_id": run_id, **payload})


def _resolve_parent_id(
    parent_key_raw: str, cases: Sequence[CaseRecord], *, exclude_id: int
) -> int | None:
    for c in cases:
        if c.id == exclude_id:
            continue
        if keys_equivalent(parent_key_raw, c.case_key):
            return int(c.id)
    return None


def _build_case_summary(
    case: CaseRecord,
    links: Sequence[CaseRunLink],
    facts_by_run: Mapping[int, RunExecutionFacts],
    *,
    parent_key: str | None,
    children_keys: Sequence[str],
) -> CaseSummary:
    runs = tuple(facts_by_run[lk.run_id] for lk in links if lk.run_id in facts_by_run)
    latest = select_governing_run(runs)
    return CaseSummary(
        case=case,
        links=tuple(links),
        runs=runs,
        latest=latest,
        parent_case_key=parent_key,
        children_keys=tuple(children_keys),
    )


class CampaignExcelPlugin(Plugin):
    name = "campaign-excel"
    provides = contract.MANAGER_SERVICE_KEY
    contract_version = contract.MANAGER_CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        storage_contract.SERVICE_KEY: storage_contract.CONTRACT_VERSION,
        ts_contract.SERVICE_KEY: ts_contract.CONTRACT_VERSION,
        contract.SERVICE_KEY: contract.CONTRACT_VERSION,
    }
    config_schema = CampaignExcelConfig
    description = "Excel campaign import/export, run matching on monitor.polled, and case states."

    def apply(self, ctx: Context) -> CampaignManager:
        assert isinstance(ctx.config, CampaignExcelConfig)
        storage: Storage = ctx.services.require(storage_contract.SERVICE_KEY)
        ts: TimeSeriesStore = ctx.services.require(ts_contract.SERVICE_KEY)
        store: CampaignStore = ctx.services.require(contract.SERVICE_KEY)
        manager = CampaignManager(ctx, storage, ts, store, ctx.config)
        ctx.on(monitor_contract.POLLED_EVENT, manager.on_monitor_polled)
        for kind in (
            "run.solver_state_changed",
            "solver.finished",
            "solver.error",
            "solver.nan_detected",
        ):
            ctx.on(kind, manager.on_run_event)
        return manager
