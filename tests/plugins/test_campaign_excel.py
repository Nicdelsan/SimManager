from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
import pytest

from simmanager.contracts import campaign as campaign_contract
from simmanager.contracts import monitor as monitor_contract
from simmanager.contracts.campaign import (
    CampaignSpec,
    CaseComputedState,
    CaseSpec,
    ExportOptions,
    ImportOptions,
    MatchKind,
)
from simmanager.contracts.monitor import PollReport
from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.contracts.storage import Storage
from simmanager.contracts.timeseries import RunSolverState, SeriesKey, SolverState, TimeSeriesStore
from simmanager.kernel import Kernel
from simmanager.plugins.campaign_excel import CampaignExcelPlugin
from simmanager.plugins.storage_sqlite import (
    SqliteCampaignPlugin,
    SqliteStoragePlugin,
    SqliteTimeSeriesPlugin,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_XLSX = ROOT / "samples" / "cases_run_campaign.xlsx"


def _build_campaign_xlsx(path: Path) -> Path:
    """Create an anonymized multi-sheet campaign workbook mirroring real layout quirks."""
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Cases"

    # Columns A..L have headers; M is an empty spacer; N has headerless notes.
    ws.append(
        [
            "# Case",
            "Liquid Solver",
            "Species",
            "VOF",
            "State",
            "Diverged",
            "Ref. Folder",
        ]
    )
    ws.append([1, "Incompressible", "CH3OH", "/", "Failed", "Yes", "/work/user1/proj_a/001_base"])
    ws.append(
        [
            2,
            "Compressible",
            "CH3OH",
            "Yes - Cav. (HRM)",
            "Done",
            "No",
            "/work/user1/proj_a/002_001_comp",
        ]
    )
    ws.append(
        [
            3,
            "Compressible",
            "N2",
            "Yes - Cav. (SS)",
            "Running",
            "",
            "/work/user1/proj_a/003_002_ss",
        ]
    )
    ws.append([4, "Compressible", "CH3OH", "", "Pending", "", ""])

    # Headerless annotations in column N (column 14, leaving M empty).
    ws.cell(row=2, column=14, value="Baseline")
    ws.cell(row=3, column=14, value="Changed Param.")

    # Blank spacer rows + trailing free-text notes below the table.
    ws.cell(row=7, column=1, value="note: check mesh near valve")
    ws.cell(row=8, column=1, value="note: restart transient")

    ws2 = wb.create_sheet("Comparisons")
    ws2.append(["Comparison", "Cases", None, None])
    ws2.merge_cells("B1:D1")
    ws2.append(["Turbulence", 1, 2])

    wb.save(path)
    wb.close()
    return path


async def _make_kernel(db_path: Path, cfg: dict[str, Any] | None = None) -> Kernel:
    kernel = Kernel(
        {
            "storage-sqlite": {"path": str(db_path)},
            "campaign-excel": cfg or {},
        }
    )
    await kernel.add(
        SqliteStoragePlugin,
        SqliteTimeSeriesPlugin,
        SqliteCampaignPlugin,
        CampaignExcelPlugin,
    )
    return kernel


async def test_preview_import_reimport_rename_and_missing(tmp_path: Path) -> None:
    xlsx = _build_campaign_xlsx(tmp_path / "camp.xlsx")
    kernel = await _make_kernel(tmp_path / "sim.db")
    try:
        mgr = kernel.services.require(campaign_contract.MANAGER_SERVICE_KEY)
        store = kernel.services.require(campaign_contract.SERVICE_KEY)
        storage: Storage = kernel.services.require("storage")

        preview = await mgr.preview_xlsx(xlsx)
        assert preview.sheets == ("Cases", "Comparisons")
        assert preview.selected_sheet == "Cases"
        assert preview.header_row == 1
        assert preview.detected_case_column == "# Case"
        assert preview.detected_work_dir_column == "Ref. Folder"
        assert preview.detected_manual_state_column == "State"
        assert preview.total_data_rows == 4
        assert "col_N" in preview.columns
        assert "col_M" not in preview.columns

        rep1 = await mgr.import_xlsx(xlsx, ImportOptions(project_name="p1", campaign_name="c1"))
        assert rep1.added == ("1", "2", "3", "4")
        assert rep1.updated == ()
        assert rep1.missing == ()

        camp, summaries = await mgr.show_campaign("c1", project_name="p1")
        assert camp.work_root == "/work/user1/proj_a"
        by_key = {s.case.case_key: s for s in summaries}
        assert by_key["2"].case.manual_state == "Done"
        assert by_key["2"].case.params["VOF"] == "Yes - Cav. (HRM)"
        assert by_key["2"].case.params["Diverged"] == "No"
        assert by_key["2"].case.params["col_N"] == "Changed Param."
        assert by_key["1"].case.params["VOF"] == "/"
        assert by_key["2"].parent_case_key == "1"
        assert by_key["3"].parent_case_key == "2"

        # Idempotent reimport.
        rep2 = await mgr.import_xlsx(xlsx, ImportOptions(project_name="p1", campaign_name="c1"))
        assert rep2.added == ()
        assert rep2.updated == ()
        assert rep2.unchanged == ("1", "2", "3", "4")

        # Link a run to case 2 before testing rename + missing.
        up = await storage.upsert_job(
            "cluster-a",
            JobInfo(
                job_id="10",
                name="002_001_comp",
                state=JobState.COMPLETED,
                raw_state="COMPLETED",
                work_dir="/work/user1/proj_a/002_001_comp",
            ),
            active=False,
        )
        assert up.record.run_id is not None
        await mgr.reconcile_all()

        # Modify workbook: update case 1, rename case 2 -> 2b (same work_dir), drop case 4.
        wb = openpyxl.load_workbook(xlsx)
        ws = wb["Cases"]
        ws["B2"] = "Compressible"
        ws["A3"] = "2b"
        ws.delete_rows(5, 1)  # remove case 4
        wb.save(xlsx)
        wb.close()

        dry = await mgr.import_xlsx(
            xlsx, ImportOptions(project_name="p1", campaign_name="c1", dry_run=True)
        )
        assert dry.renamed == (("2", "2b"),)
        assert "1" in dry.updated
        assert "4" in dry.missing

        rep3 = await mgr.import_xlsx(
            xlsx,
            ImportOptions(project_name="p1", campaign_name="c1", apply_renames=True),
        )
        assert rep3.renamed == (("2", "2b"),)
        assert rep3.updated == ("1",)
        assert rep3.missing == ("4",)

        # Case 2b kept its linked run and case 4 is inactive (not deleted).
        _, summary_2b = await mgr.show_case("c1", "2b", project_name="p1")
        assert len(summary_2b.runs) == 1
        inactive_4 = await store.case(camp.id, "4")
        assert inactive_4 is not None and inactive_4.active is False

        events = await storage.events(kinds=[campaign_contract.EVENT_CAMPAIGN_IMPORTED])
        assert len(events) == 3
    finally:
        await kernel.stop()


async def test_two_campaigns_same_numbering_scoped_by_work_root(tmp_path: Path) -> None:
    kernel = await _make_kernel(tmp_path / "sim.db")
    try:
        storage: Storage = kernel.services.require("storage")
        store = kernel.services.require(campaign_contract.SERVICE_KEY)
        mgr = kernel.services.require(campaign_contract.MANAGER_SERVICE_KEY)

        ca = await store.upsert_campaign(
            CampaignSpec(project_name="pa", name="camp_a", work_root="/work/user1/proj_a")
        )
        await store.upsert_case(
            ca.id, CaseSpec(case_key="1", work_dir="/work/user1/proj_a/001_base")
        )

        cb = await store.upsert_campaign(
            CampaignSpec(project_name="pb", name="camp_b", work_root="/work/user1/proj_b")
        )
        await store.upsert_case(
            cb.id, CaseSpec(case_key="1", work_dir="/work/user1/proj_b/001_base")
        )

        # Campaign without work_root: only auto_work_dir may match.
        cc = await store.upsert_campaign(
            CampaignSpec(project_name="pc", name="camp_noroot", work_root=None)
        )
        await store.upsert_case(cc.id, CaseSpec(case_key="1", work_dir=None))

        # Run inside proj_a with renamed folder 001_restart -> matches ONLY camp_a.
        r_a = await storage.upsert_job(
            "c1",
            JobInfo(
                job_id="101",
                name="001_restart",
                state=JobState.RUNNING,
                raw_state="RUNNING",
                work_dir="/work/user1/proj_a/001_restart",
            ),
            active=True,
        )
        # Run in unrelated project proj_x -> matches NONE.
        r_x = await storage.upsert_job(
            "c1",
            JobInfo(
                job_id="102",
                name="001_other",
                state=JobState.RUNNING,
                raw_state="RUNNING",
                work_dir="/work/user1/proj_x/001_other",
            ),
            active=True,
        )

        await mgr.reconcile_all()

        links_a = await store.links_for_campaign(ca.id)
        links_b = await store.links_for_campaign(cb.id)
        links_c = await store.links_for_campaign(cc.id)
        assert [lk.run_id for lk in links_a] == [r_a.record.run_id]
        assert links_a[0].match_kind is MatchKind.AUTO_REGEX
        assert links_b == []
        assert links_c == []

        unmatched_a = await mgr.unmatched("camp_a", project_name="pa")
        assert [u.run.id for u in unmatched_a.unmatched_runs] == [r_x.record.run_id]
    finally:
        await kernel.stop()


async def test_ambiguity_manual_match_exclusion_and_monitor_polled(tmp_path: Path) -> None:
    kernel = await _make_kernel(tmp_path / "sim.db")
    try:
        storage: Storage = kernel.services.require("storage")
        store = kernel.services.require(campaign_contract.SERVICE_KEY)
        mgr = kernel.services.require(campaign_contract.MANAGER_SERVICE_KEY)

        camp = await store.upsert_campaign(
            CampaignSpec(project_name="p", name="amb", work_root="/work/user1/amb")
        )
        # Case "1" and "01" both match regex token "001" numerically -> ambiguity!
        await store.upsert_case(camp.id, CaseSpec(case_key="1", work_dir="/work/user1/amb/001_v1"))
        await store.upsert_case(camp.id, CaseSpec(case_key="01", work_dir="/work/user1/amb/001_v2"))

        up = await storage.upsert_job(
            "cl",
            JobInfo(
                job_id="201",
                name="001_test",
                state=JobState.QUEUED,
                raw_state="PENDING",
                work_dir="/work/user1/amb/001_test",
            ),
            active=True,
        )
        run_id = up.record.run_id
        assert run_id is not None

        # Simulate monitor.polled bus event twice: ambiguity event recorded once.
        await kernel.bus.emit(
            monitor_contract.POLLED_EVENT, PollReport(cluster_id="cl", ok=True, discovered=1)
        )
        await kernel.bus.emit(monitor_contract.POLLED_EVENT, PollReport(cluster_id="cl", ok=True))

        assert await store.links_for_campaign(camp.id) == []
        amb_events = await storage.events(kinds=[campaign_contract.EVENT_AMBIGUOUS_MATCH])
        assert len(amb_events) == 1
        assert set(amb_events[0].payload["candidate_cases"]) == {"1", "01"}

        unm = await mgr.unmatched("amb", project_name="p")
        assert len(unm.unmatched_runs) == 1
        assert set(unm.unmatched_runs[0].ambiguous_case_keys) == {"1", "01"}

        # Manual match locks the link and survives subsequent polls.
        link = await mgr.match_run("amb", "01", run_id, project_name="p")
        assert link.locked is True
        await kernel.bus.emit(monitor_contract.POLLED_EVENT, PollReport(cluster_id="cl", ok=True))
        links = await store.links_for_campaign(camp.id)
        assert len(links) == 1 and links[0].match_kind is MatchKind.MANUAL

        # Manual unmatch adds exclusion so auto-match does not re-link it.
        await mgr.unmatch_run("amb", run_id, project_name="p")
        await kernel.bus.emit(monitor_contract.POLLED_EVENT, PollReport(cluster_id="cl", ok=True))
        assert await store.links_for_campaign(camp.id) == []
    finally:
        await kernel.stop()


async def test_multi_run_and_computed_states(tmp_path: Path) -> None:
    kernel = await _make_kernel(tmp_path / "sim.db")
    try:
        storage: Storage = kernel.services.require("storage")
        ts: TimeSeriesStore = kernel.services.require("timeseries")
        store = kernel.services.require(campaign_contract.SERVICE_KEY)
        mgr = kernel.services.require(campaign_contract.MANAGER_SERVICE_KEY)

        camp = await store.upsert_campaign(
            CampaignSpec(project_name="p", name="states", work_root="/work/user1/s")
        )
        c_multi, _, _ = await store.upsert_case(
            camp.id, CaseSpec(case_key="1", work_dir="/work/user1/s/001")
        )
        c_div, _, _ = await store.upsert_case(
            camp.id, CaseSpec(case_key="2", work_dir="/work/user1/s/002")
        )
        c_stop, _, _ = await store.upsert_case(
            camp.id, CaseSpec(case_key="3", work_dir="/work/user1/s/003")
        )
        c_unk, _, _ = await store.upsert_case(
            camp.id, CaseSpec(case_key="4", work_dir="/work/user1/s/004")
        )
        c_can, _, _ = await store.upsert_case(
            camp.id, CaseSpec(case_key="5", work_dir="/work/user1/s/005")
        )

        # Case 1: completed run + newer active queued run -> queued, then running.
        r1_old = await storage.upsert_job(
            "cl",
            JobInfo(
                job_id="1",
                name="001",
                state=JobState.COMPLETED,
                raw_state="COMPLETED",
                work_dir="/work/user1/s/001",
            ),
            active=False,
        )
        r1_new = await storage.upsert_job(
            "cl",
            JobInfo(
                job_id="2",
                name="001",
                state=JobState.QUEUED,
                raw_state="PENDING",
                work_dir="/work/user1/s/001",
            ),
            active=True,
        )
        assert r1_old.record.run_id and r1_new.record.run_id

        # Case 2: diverged via solver.nan_detected + FAILED.
        r2 = await storage.upsert_job(
            "cl",
            JobInfo(
                job_id="3",
                name="002",
                state=JobState.FAILED,
                raw_state="FAILED",
                work_dir="/work/user1/s/002",
            ),
            active=False,
        )
        assert r2.record.run_id
        await ts.upsert_run_solver_state(
            RunSolverState(
                run_id=r2.record.run_id,
                solver="converge",
                work_dir="/work/user1/s/002",
                solver_state=SolverState.FAILED,
            )
        )
        await storage.append_event(
            "solver.nan_detected", {}, source="test", run_id=r2.record.run_id
        )

        # Case 3: CONVERGE STOP -> stopped.
        r3 = await storage.upsert_job(
            "cl",
            JobInfo(
                job_id="4",
                name="003",
                state=JobState.COMPLETED,
                raw_state="COMPLETED",
                work_dir="/work/user1/s/003",
            ),
            active=False,
        )
        assert r3.record.run_id
        await ts.upsert_run_solver_state(
            RunSolverState(
                run_id=r3.record.run_id,
                solver="converge",
                work_dir="/work/user1/s/003",
                solver_state=SolverState.STOPPED,
            )
        )

        # Case 4: UNKNOWN job state with no solver state -> unknown.
        await storage.upsert_job(
            "cl",
            JobInfo(
                job_id="5",
                name="004",
                state=JobState.UNKNOWN,
                raw_state="",
                work_dir="/work/user1/s/004",
            ),
            active=False,
        )

        # Case 5: CANCELLED -> cancelled.
        await storage.upsert_job(
            "cl",
            JobInfo(
                job_id="6",
                name="005",
                state=JobState.CANCELLED,
                raw_state="CANCELLED",
                work_dir="/work/user1/s/005",
            ),
            active=False,
        )

        await mgr.reconcile_all()

        assert (await store.case(camp.id, c_multi.id)).computed_state is CaseComputedState.QUEUED  # type: ignore[union-attr]
        assert (await store.case(camp.id, c_div.id)).computed_state is CaseComputedState.DIVERGED  # type: ignore[union-attr]
        assert (await store.case(camp.id, c_stop.id)).computed_state is CaseComputedState.STOPPED  # type: ignore[union-attr]
        assert (await store.case(camp.id, c_unk.id)).computed_state is CaseComputedState.UNKNOWN  # type: ignore[union-attr]
        assert (await store.case(camp.id, c_can.id)).computed_state is CaseComputedState.CANCELLED  # type: ignore[union-attr]

        state_events = await storage.events(kinds=[campaign_contract.EVENT_CASE_STATE_CHANGED])
        assert len(state_events) == 5
    finally:
        await kernel.stop()


async def test_export_places_sm_columns_after_max_column_and_preserves_original(
    tmp_path: Path,
) -> None:
    xlsx = _build_campaign_xlsx(tmp_path / "source.xlsx")
    out_xlsx = tmp_path / "exported.xlsx"
    kernel = await _make_kernel(
        tmp_path / "sim.db",
        cfg={"export_monitors": ["thermo.out:Pressure"]},
    )
    try:
        storage: Storage = kernel.services.require("storage")
        ts: TimeSeriesStore = kernel.services.require("timeseries")
        mgr = kernel.services.require(campaign_contract.MANAGER_SERVICE_KEY)

        await mgr.import_xlsx(xlsx, ImportOptions(project_name="p", campaign_name="exp"))
        up = await storage.upsert_job(
            "uni-a",
            JobInfo(
                job_id="777",
                name="002_001_comp",
                state=JobState.RUNNING,
                raw_state="RUNNING",
                work_dir="/work/user1/proj_a/002_001_comp",
            ),
            active=True,
        )
        run_id = up.record.run_id
        assert run_id is not None
        await ts.upsert_run_solver_state(
            RunSolverState(
                run_id=run_id,
                solver="converge",
                work_dir="/work/user1/proj_a/002_001_comp",
                solver_state=SolverState.RUNNING,
                progress=0.45,
            )
        )
        await ts.append_samples(
            run_id,
            {
                SeriesKey(file_path="outputs_0/thermo.out", name="Pressure", unit="MPa"): (
                    [0.01, 0.02],
                    [1.2, 3.45],
                )
            },
        )
        await mgr.reconcile_all()

        # Refuses to overwrite the original workbook.
        with pytest.raises(ValueError, match="refusing to overwrite"):
            await mgr.export_xlsx("exp", ExportOptions(output_path=xlsx), project_name="p")

        rep = await mgr.export_xlsx("exp", ExportOptions(output_path=out_xlsx), project_name="p")
        assert rep.used_template is True
        assert rep.cases_exported == 4

        wb_out = openpyxl.load_workbook(out_xlsx)
        assert wb_out.sheetnames == ["Cases", "Comparisons"]
        ws_out = wb_out["Cases"]
        # Original max_column was 14 (col N had headerless notes); SM_* must start at col 15 (O).
        assert ws_out.cell(row=2, column=14).value == "Baseline"
        assert ws_out.cell(row=3, column=14).value == "Changed Param."
        assert ws_out.cell(row=1, column=15).value == "SM_ComputedState"
        assert ws_out.cell(row=1, column=16).value == "SM_JobID"
        assert ws_out.cell(row=1, column=17).value == "SM_Cluster"
        assert ws_out.cell(row=1, column=18).value == "SM_Progress"
        assert ws_out.cell(row=1, column=19).value == "SM_Monitor_thermo.out:Pressure"

        # Case 2 (row 3) has live job/progress/monitor; manual State (col 5) untouched.
        assert ws_out.cell(row=3, column=5).value == "Done"
        assert ws_out.cell(row=3, column=15).value == "running"
        assert ws_out.cell(row=3, column=16).value == "777"
        assert ws_out.cell(row=3, column=17).value == "uni-a"
        assert ws_out.cell(row=3, column=18).value == 45.0
        assert ws_out.cell(row=3, column=19).value == 3.45

        # Trailing note rows untouched and have no SM_State.
        assert ws_out.cell(row=7, column=1).value == "note: check mesh near valve"
        assert ws_out.cell(row=7, column=15).value is None

        # Comparisons sheet preserved with merge.
        assert "B1:D1" in [str(r) for r in wb_out["Comparisons"].merged_cells.ranges]
        wb_out.close()

        # Original workbook untouched (still max_column == 14).
        wb_orig = openpyxl.load_workbook(xlsx)
        assert wb_orig["Cases"].max_column == 14
        wb_orig.close()

        # Fallback export when template file no longer exists on disk.
        xlsx.unlink()
        fresh_out = tmp_path / "fresh.xlsx"
        rep2 = await mgr.export_xlsx("exp", ExportOptions(output_path=fresh_out), project_name="p")
        assert rep2.used_template is False
        wb_fresh = openpyxl.load_workbook(fresh_out)
        assert "SM_ComputedState" in [c.value for c in wb_fresh["Cases"][1]]
        wb_fresh.close()
    finally:
        await kernel.stop()


@pytest.mark.slow
@pytest.mark.skipif(not SAMPLE_XLSX.is_file(), reason="local sample xlsx not present")
async def test_real_sample_campaign_import_and_export(tmp_path: Path) -> None:
    kernel = await _make_kernel(tmp_path / "real.db")
    try:
        mgr = kernel.services.require(campaign_contract.MANAGER_SERVICE_KEY)
        preview = await mgr.preview_xlsx(SAMPLE_XLSX)
        assert preview.selected_sheet == "Cases"
        assert preview.detected_case_column == "# Case"
        assert preview.detected_manual_state_column == "State"
        assert preview.detected_work_dir_column is not None
        assert preview.total_data_rows == 18

        rep = await mgr.import_xlsx(SAMPLE_XLSX, ImportOptions(campaign_name="real_sample"))
        assert len(rep.added) == 18
        camp, summaries = await mgr.show_campaign("real_sample")
        assert camp.work_root is not None
        assert any(s.parent_case_key is not None for s in summaries)

        out = tmp_path / "real_exported.xlsx"
        exp = await mgr.export_xlsx("real_sample", ExportOptions(output_path=out))
        assert exp.used_template is True
        wb = openpyxl.load_workbook(out)
        assert "Comparisons" in wb.sheetnames
        assert wb["Cases"].cell(row=1, column=15).value == "SM_ComputedState"
        wb.close()
    finally:
        await kernel.stop()
