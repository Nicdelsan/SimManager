from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from simmanager.cli.main import app

FIXTURES_ROOT = Path(__file__).resolve().parents[0] / "fixtures"
runner = CliRunner()


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SIMMANAGER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SIMMANAGER_CONFIG_DIR", str(config_dir))


def test_cli_ingest_help() -> None:
    res = runner.invoke(app, ["ingest", "--help"])
    assert res.exit_code == 0
    assert "local" in res.stdout
    assert "status" in res.stdout
    assert "series" in res.stdout


def test_cli_ingest_converge_status_and_series(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)

    # Omit --run-id -> creates a manual run in `runs` (id=1)
    res = runner.invoke(app, ["ingest", "local", str(FIXTURES_ROOT / "converge")])
    assert res.exit_code == 0, res.stdout
    assert "Ingested run 1" in res.stdout
    assert "converge" in res.stdout
    assert "completed" in res.stdout

    # Re-ingesting with --run-id 1 reuses the existing run cleanly
    res_again = runner.invoke(
        app, ["ingest", "local", str(FIXTURES_ROOT / "converge"), "--run-id", "1"]
    )
    assert res_again.exit_code == 0, res_again.stdout

    st = runner.invoke(app, ["ingest", "status", "1"])
    assert st.exit_code == 0, st.stdout
    assert "thermo.out" in st.stdout
    assert "done" in st.stdout

    sr = runner.invoke(app, ["ingest", "series", "1", "--name", "Mean_Temp", "--points"])
    assert sr.exit_code == 0, sr.stdout
    assert "Mean_Temp" in sr.stdout
    assert "level=0" in sr.stdout


def test_cli_ingest_openfoam_and_missing_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)

    res = runner.invoke(
        app,
        ["ingest", "local", str(FIXTURES_ROOT / "openfoam"), "--solver", "openfoam"],
    )
    assert res.exit_code == 0, res.stdout
    assert "Ingested run 1" in res.stdout
    assert "openfoam" in res.stdout

    sr = runner.invoke(
        app, ["ingest", "series", "1", "--name", "deltaT", "--points", "--limit", "5"]
    )
    assert sr.exit_code == 0, sr.stdout
    assert "deltaT" in sr.stdout

    missing_dir = runner.invoke(app, ["ingest", "local", str(tmp_path / "does-not-exist")])
    assert missing_dir.exit_code == 2

    missing_manual_run = runner.invoke(
        app, ["ingest", "local", str(FIXTURES_ROOT / "openfoam"), "--run-id", "999"]
    )
    assert missing_manual_run.exit_code == 1

    missing_run = runner.invoke(app, ["ingest", "status", "999"])
    assert missing_run.exit_code == 1
