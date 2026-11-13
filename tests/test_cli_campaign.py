from __future__ import annotations

import asyncio
from pathlib import Path

import openpyxl
import pytest
from typer.testing import CliRunner

from simmanager import paths
from simmanager.cli.main import app
from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.plugins.storage_sqlite import SqliteStorage

runner = CliRunner()


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SIMMANAGER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SIMMANAGER_CONFIG_DIR", str(config_dir))


def _make_xlsx(path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Cases"
    ws.append(["# Case", "Solver", "State", "Ref. Folder"])
    ws.append([1, "Incompressible", "Done", "/work/user1/camp/001_base"])
    ws.append([2, "Compressible", "Pending", "/work/user1/camp/002_001_comp"])
    ws.cell(row=2, column=6, value="Baseline note")
    wb.save(path)
    wb.close()
    return path


def test_cli_campaign_help() -> None:
    res = runner.invoke(app, ["campaign", "--help"])
    assert res.exit_code == 0
    for cmd in ("import", "list", "show", "match", "unmatch", "unmatched", "export"):
        assert cmd in res.stdout


def test_cli_campaign_workflow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)
    xlsx = _make_xlsx(tmp_path / "demo_campaign.xlsx")

    # Seed an unmatched run in SQLite before importing.
    async def seed() -> tuple[int, int]:
        st = await SqliteStorage.open(str(paths.default_database_file()))
        u1 = await st.upsert_job(
            "uni-a",
            JobInfo(
                job_id="501",
                name="001_base",
                state=JobState.COMPLETED,
                raw_state="COMPLETED",
                work_dir="/work/user1/camp/001_base",
            ),
            active=False,
        )
        u2 = await st.upsert_job(
            "uni-a",
            JobInfo(
                job_id="502",
                name="unrelated",
                state=JobState.RUNNING,
                raw_state="RUNNING",
                work_dir="/work/user1/other/999_extra",
            ),
            active=True,
        )
        await st.close()
        assert u1.record.run_id and u2.record.run_id
        return u1.record.run_id, u2.record.run_id

    _, r2_id = asyncio.run(seed())

    # Interactive confirmation on import (accepting auto-detected defaults).
    res_imp = runner.invoke(
        app,
        ["campaign", "import", str(xlsx), "--name", "demo"],
        input="\n\n\n\n\ny\n",
    )
    assert res_imp.exit_code == 0, res_imp.stdout
    assert "Workbook preview" in res_imp.stdout
    assert "matched_runs" in res_imp.stdout

    res_list = runner.invoke(app, ["campaign", "list"])
    assert res_list.exit_code == 0, res_list.stdout
    assert "demo" in res_list.stdout

    res_show = runner.invoke(app, ["campaign", "show", "demo"], env={"COLUMNS": "200"})
    assert res_show.exit_code == 0, res_show.stdout
    assert "completed" in res_show.stdout
    assert "planned" in res_show.stdout

    res_all = runner.invoke(
        app, ["campaign", "show", "demo", "--all-params"], env={"COLUMNS": "200"}
    )
    assert res_all.exit_code == 0, res_all.stdout
    assert "Incompressible" in res_all.stdout
    assert "Baseline note" in res_all.stdout

    res_case = runner.invoke(app, ["campaign", "show", "demo", "--case", "2"])
    assert res_case.exit_code == 0, res_case.stdout
    assert "parent" in res_case.stdout

    res_unm = runner.invoke(app, ["campaign", "unmatched", "demo"], env={"COLUMNS": "200"})
    assert res_unm.exit_code == 0, res_unm.stdout
    assert "unrelated" in res_unm.stdout

    res_match = runner.invoke(app, ["campaign", "match", "demo", "2", str(r2_id)])
    assert res_match.exit_code == 0, res_match.stdout
    assert "matched" in res_match.stdout

    res_unmatch = runner.invoke(app, ["campaign", "unmatch", "demo", str(r2_id)])
    assert res_unmatch.exit_code == 0, res_unmatch.stdout
    assert "unmatched" in res_unmatch.stdout

    out_xlsx = tmp_path / "demo_out.xlsx"
    res_exp = runner.invoke(app, ["campaign", "export", "demo", "-o", str(out_xlsx)])
    assert res_exp.exit_code == 0, res_exp.stdout
    assert out_xlsx.is_file()
    wb = openpyxl.load_workbook(out_xlsx)
    # Headerless note was in col 6 (F); SM_ComputedState must start at col 7 (G).
    assert wb["Cases"].cell(row=1, column=7).value == "SM_ComputedState"
    wb.close()

    # Exporting onto source fails cleanly.
    res_overwrite = runner.invoke(app, ["campaign", "export", "demo", "-o", str(xlsx)])
    assert res_overwrite.exit_code == 2
