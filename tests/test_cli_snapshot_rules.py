from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from simmanager import paths
from simmanager.cli.main import app
from simmanager.contracts.campaign import CampaignSpec, CaseSpec
from simmanager.contracts.rules import AlertRaise, RuleKind, RuleSeverity
from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.plugins.notifier import desktop as desktop_mod
from simmanager.plugins.storage_sqlite import SqliteCampaignStore, SqliteRuleStore, SqliteStorage

FIXTURES_ROOT = Path(__file__).resolve().parents[0] / "fixtures"
runner = CliRunner()


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SIMMANAGER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SIMMANAGER_CONFIG_DIR", str(config_dir))


def test_cli_m6_help() -> None:
    for sub in ("snapshot", "run", "rules", "alerts", "notify"):
        res = runner.invoke(app, [sub, "--help"])
        assert res.exit_code == 0, f"{sub} --help failed: {res.stdout}"


def test_cli_snapshot_diff_and_run_note(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)

    case1 = tmp_path / "run1"
    case2 = tmp_path / "run2"
    case1.mkdir()
    case2.mkdir()
    shutil.copy(FIXTURES_ROOT / "converge" / "solver_v1.in", case1 / "solver.in")
    shutil.copy(FIXTURES_ROOT / "converge" / "solver_v2.in", case2 / "solver.in")

    async def seed() -> tuple[int, int]:
        st = await SqliteStorage.open(str(paths.default_database_file()))
        u1 = await st.upsert_job(
            "uni-a",
            JobInfo(
                job_id="701",
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
                job_id="702",
                name="002_fine",
                state=JobState.RUNNING,
                raw_state="RUNNING",
                work_dir="/work/user1/camp/002_fine",
            ),
            active=True,
        )
        await st.close()
        assert u1.record.run_id and u2.record.run_id
        return u1.record.run_id, u2.record.run_id

    r1, r2 = asyncio.run(seed())

    t1 = runner.invoke(
        app,
        ["snapshot", "take", str(r1), "--local", str(case1), "--solver", "converge"],
    )
    assert t1.exit_code == 0, t1.stdout
    assert "Captured snapshot #" in t1.stdout

    t2 = runner.invoke(
        app,
        ["snapshot", "take", str(r2), "--local", str(case2), "--solver", "converge"],
    )
    assert t2.exit_code == 0, t2.stdout
    assert "Captured snapshot #" in t2.stdout

    ls = runner.invoke(app, ["snapshot", "list", str(r1)])
    assert ls.exit_code == 0, ls.stdout
    assert "converge" in ls.stdout

    sh = runner.invoke(app, ["snapshot", "show", "1"])
    assert sh.exit_code == 0, sh.stdout
    assert "solver.in" in sh.stdout

    sh_file = runner.invoke(app, ["snapshot", "show", "1", "--file", "solver.in"])
    assert sh_file.exit_code == 0, sh_file.stdout
    assert "turbulence_model" in sh_file.stdout

    df = runner.invoke(app, ["snapshot", "diff", str(r2), "--against", str(r1), "--unified"])
    assert df.exit_code == 0, df.stdout
    assert "Semantic changes" in df.stdout
    assert "solver.in" in df.stdout

    # Run note set / read / clear
    n_set = runner.invoke(app, ["run", "note", str(r1), "Baseline mesh run"])
    assert n_set.exit_code == 0, n_set.stdout
    assert "Saved note" in n_set.stdout

    n_get = runner.invoke(app, ["run", "note", str(r1)])
    assert n_get.exit_code == 0, n_get.stdout
    assert "Baseline mesh run" in n_get.stdout

    n_clr = runner.invoke(app, ["run", "note", str(r1), "--clear"])
    assert n_clr.exit_code == 0, n_clr.stdout
    assert "Cleared note" in n_clr.stdout


def test_cli_rules_alerts_and_notify(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)

    async def seed() -> int:
        st = await SqliteStorage.open(str(paths.default_database_file()))
        camp_store = SqliteCampaignStore(st.connection, st.write_lock)
        camp = await camp_store.upsert_campaign(CampaignSpec(project_name="default", name="demo"))
        await camp_store.upsert_case(
            camp.id, CaseSpec(case_key="001_base", work_dir="/work/u/demo/001_base")
        )
        rule_store = SqliteRuleStore(st.connection, st.write_lock)
        alert, _ = await rule_store.raise_alert(
            AlertRaise(
                dedup_key="test:1:job_failed",
                rule_key="job_failed",
                rule_kind=RuleKind.JOB_FAILED,
                severity=RuleSeverity.CRITICAL,
                title="Job failed",
                message="SLURM job exited non-zero",
            )
        )
        await st.close()
        return alert.id

    alert_id = asyncio.run(seed())

    r_show = runner.invoke(app, ["rules", "show"])
    assert r_show.exit_code == 0, r_show.stdout
    assert "residual_high" in r_show.stdout

    r_set = runner.invoke(
        app,
        [
            "rules",
            "set",
            "courant_high",
            "--campaign",
            "demo",
            "--case",
            "001_base",
            "--severity",
            "critical",
            "--param",
            "max=0.5",
        ],
    )
    assert r_set.exit_code == 0, r_set.stdout
    assert "Saved rule courant_high at case:" in r_set.stdout

    r_show_case = runner.invoke(app, ["rules", "show", "--campaign", "demo", "--case", "001_base"])
    assert r_show_case.exit_code == 0, r_show_case.stdout
    assert "0.5" in r_show_case.stdout

    r_unset = runner.invoke(
        app,
        ["rules", "unset", "courant_high", "--campaign", "demo", "--case", "001_base"],
    )
    assert r_unset.exit_code == 0, r_unset.stdout
    assert "Removed rule override courant_high" in r_unset.stdout

    a_list = runner.invoke(app, ["alerts", "list", "--open"])
    assert a_list.exit_code == 0, a_list.stdout
    assert "Job failed" in a_list.stdout

    a_ack = runner.invoke(app, ["alerts", "ack", str(alert_id), "--by", "tester"])
    assert a_ack.exit_code == 0, a_ack.stdout
    assert f"Acknowledged alert #{alert_id}" in a_ack.stdout

    a_open_after = runner.invoke(app, ["alerts", "list", "--open"])
    assert a_open_after.exit_code == 0, a_open_after.stdout
    assert "tester" in a_open_after.stdout

    # Mock DesktopNotifierProvider.available and .send so this test is OS/headless-independent
    # and never triggers a real desktop notification.
    sent: list[object] = []

    def _fake_available(self: object) -> bool:
        return True

    async def _fake_send(self: object, message: object) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(desktop_mod.DesktopNotifierProvider, "available", _fake_available)
    monkeypatch.setattr(desktop_mod.DesktopNotifierProvider, "send", _fake_send)
    n_test = runner.invoke(app, ["notify", "test"])
    assert n_test.exit_code == 0, n_test.stdout
    assert "desktop: sent" in n_test.stdout
    assert len(sent) == 1
